"""
EVrota AI asistanı: POST /agent/chat

Gemini "function calling" ile çalışır. Model kendi bilgisinden uydurmak yerine
EVrota'nın gerçek verisini araç (tool) olarak çağırır:
  - yakin_istasyonlar   : kullanıcının konumuna göre şarj istasyonları + anlık doluluk
  - arac_ara            : katalogda araç arama / filtreleme (menzil, kasa, batarya)
  - arac_karsilastir    : 2-3 aracı teknik verilerle yan yana
  - yolculuk_hesapla    : mesafe + araç + senaryo → şarj durağı sayısı ve süresi
  - maliyet_hesapla     : km + tüketim + kWh fiyatı → tutar

Güvenlik/maliyet: cihaz başına günlük mesaj sınırı, mesaj ve geçmiş uzunluk sınırı,
en fazla 4 araç-çağrısı turu, "thinking" kapalı (token tasarrufu).
"""
import json
import logging
import math
import time
from typing import Awaitable, Callable, Optional

import httpx

from . import cache, config, news, occupancy, places

log = logging.getLogger("evrota.agent")

SCENARIOS = {"wltp": 1.0, "yaz": 0.85, "otoyol": 0.70, "kis": 0.68, "kis_otoyol": 0.55}

SYSTEM = """Sen EVrota uygulamasının elektrikli araç asistanısın. Türkiye'deki EV sürücülerine yardım edersin.
Kurallar:
- Türkçe, samimi ve kısa yaz (genelde 2-6 cümle ya da kısa maddeler).
- İstasyon, araç, menzil, yolculuk ve maliyet sorularında MUTLAKA araçları (fonksiyonları) kullan; teknik değer uydurma.
- Katalogda olmayan araç için genel bilgi verebilirsin ama "katalogda yok, değerler yaklaşık" de.
- Fiyat (TL) tahmini yapma; araç satış fiyatı sorulursa bilmediğini söyle.
- Konum gerekiyorsa ve verilmediyse kullanıcıdan uygulamada konum iznini açmasını iste.
- Elektrikli araçlar, şarj, batarya bakımı, yolculuk planı dışındaki konularda kibarca konuya dön.
- Güvenlikle ilgili konularda (ör. arızalı soket, yanık kablo) önce güvenliği öner.
Kullanıcı bağlamı: {ctx}"""

TOOLS = [{"functionDeclarations": [
    {"name": "yakin_istasyonlar",
     "description": "Kullanıcının konumuna yakın şarj istasyonlarını ve anlık boş soket sayılarını getirir.",
     "parameters": {"type": "object", "properties": {
         "yaricap_km": {"type": "integer", "description": "Arama yarıçapı km (1-30), varsayılan 5"},
         "operator": {"type": "string", "description": "İsteğe bağlı firma: " + ", ".join(places.OPERATOR_NAMES)},
         "sadece_bos": {"type": "boolean", "description": "Sadece boş soketi olanlar"},
         "min_kw": {"type": "integer", "description": "En az güç kW (ör. DC hızlı için 50)"}}}},
    {"name": "arac_ara",
     "description": "EVrota araç kataloğunda arama/filtreleme. Marka/model metni veya kriterlerle.",
     "parameters": {"type": "object", "properties": {
         "metin": {"type": "string", "description": "Marka veya model, ör. 'togg', 'model y'"},
         "min_menzil_km": {"type": "integer"}, "kasa": {"type": "string", "description": "SUV, Sedan, Hatchback..."},
         "sirala": {"type": "string", "enum": ["menzil", "sarj", "verim"]}, "adet": {"type": "integer"}}}},
    {"name": "arac_karsilastir",
     "description": "2-3 aracı katalog verileriyle karşılaştırır (menzil senaryoları, şarj, tüketim).",
     "parameters": {"type": "object", "properties": {
         "arac_idleri": {"type": "array", "items": {"type": "string"}, "description": "Katalog id'leri (arac_ara ile bul)"}},
         "required": ["arac_idleri"]}},
    {"name": "yolculuk_hesapla",
     "description": "Bir araçla belirli mesafede kaç şarj durağı ve ne kadar şarj süresi gerektiğini hesaplar.",
     "parameters": {"type": "object", "properties": {
         "arac_id": {"type": "string"}, "mesafe_km": {"type": "integer"},
         "senaryo": {"type": "string", "enum": list(SCENARIOS)},
         "baslangic_yuzde": {"type": "integer", "description": "Çıkıştaki şarj %, varsayılan 90"}},
         "required": ["arac_id", "mesafe_km"]}},
    {"name": "maliyet_hesapla",
     "description": "Mesafe, tüketim ve kWh fiyatıyla şarj maliyetini hesaplar.",
     "parameters": {"type": "object", "properties": {
         "mesafe_km": {"type": "integer"}, "arac_id": {"type": "string"},
         "kwh_fiyat_tl": {"type": "number", "description": "Bilinmiyorsa boş bırak; DC ortalama kullanılır"}},
         "required": ["mesafe_km"]}},
]}]


def _v(x: dict) -> dict:
    cons = round(x["batteryKwh"] / x["rangeWltp"] * 100, 1)
    return {"id": x["id"], "ad": f'{x["brand"]} {x["model"]}', "kasa": x.get("body"),
            "batarya_kwh": x["batteryKwh"], "wltp_km": x["rangeWltp"],
            "gercek_yaz_km": int(x["rangeWltp"] * 0.85), "otoyol_km": int(x["rangeWltp"] * 0.70),
            "kis_otoyol_km": int(x["rangeWltp"] * 0.55), "tuketim_kwh_100km": cons,
            "dc_kw": x["dcMaxKw"], "dc_10_80_dk": x["dc10to80Min"], "ac_kw": x["acMaxKw"]}


def _find(cat: list, vid: Optional[str]) -> Optional[dict]:
    return next((x for x in cat if x["id"] == vid), None) if vid else None


def _avg_dc_price() -> Optional[float]:
    try:
        with open(config.PRICES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        ch = data.get("charging") or {}
        if isinstance(ch.get("dcAvgTlKwh"), (int, float)):
            return float(ch["dcAvgTlKwh"])
        vals = [o.get("dcTlKwh") or o.get("dc") for o in ch.get("operators", [])]
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else None
    except Exception:
        return None


async def _run_tool(name: str, args: dict, ctx: dict, stations_fn) -> dict:
    cat = news._catalog()
    if name == "yakin_istasyonlar":
        if ctx.get("lat") is None:
            return {"hata": "Konum yok. Kullanıcı uygulamada konum iznini açmalı."}
        r = max(1, min(int(args.get("yaricap_km") or 5), 30))
        op = args.get("operator")
        if op not in places.OPERATOR_NAMES:
            op = None
        try:
            payload = await stations_fn(ctx["lat"], ctx["lng"], r * 1000, op)
        except Exception:
            return {"hata": "İstasyon bilgisi şu an alınamıyor"}
        out = []
        for s in payload["stations"]:
            if args.get("sadece_bos") and not (s.get("availableCount") or 0):
                continue
            if (s.get("maxKw") or 0) < int(args.get("min_kw") or 0):
                continue
            out.append({"ad": s["name"], "firma": s.get("operator"), "mesafe_m": s["distance"],
                        "max_kw": s.get("maxKw"), "bos_soket": s.get("availableCount"), "toplam": s.get("totalCount"),
                        "soketler": [f'{c["type"]} {c["kw"]}kW x{c["count"]}' for c in s.get("connectors", [])][:4]})
        # Doluluk geçmişi: "bu saatte genelde ne kadar boş" (yeterli veri varsa)
        by_name = {s["name"]: s["id"] for s in payload["stations"]}
        for o in out[:8]:
            sid = by_name.get(o["ad"])
            pat = await occupancy.pattern(sid) if sid else None
            if pat and pat.get("enough") and pat.get("now") is not None:
                o["bu_saatte_genelde_bos_orani"] = pat["now"]
                o["bugun_en_bos_saatler"] = pat["bestHours"]
        return {"istasyonlar": out[:8], "toplam_bulunan": len(out), "yaricap_km": r}
    if name == "arac_ara":
        res = cat
        t = (args.get("metin") or "").lower().strip()
        if t:
            res = [x for x in res if all(w in f'{x["brand"]} {x["model"]}'.lower() for w in t.split())]
        if args.get("min_menzil_km"):
            res = [x for x in res if x["rangeWltp"] >= int(args["min_menzil_km"])]
        if args.get("kasa"):
            res = [x for x in res if (x.get("body") or "").lower() == args["kasa"].lower()]
        key = {"menzil": lambda x: -x["rangeWltp"], "sarj": lambda x: x["dc10to80Min"],
               "verim": lambda x: x["batteryKwh"] / x["rangeWltp"]}.get(args.get("sirala") or "menzil")
        res = sorted(res, key=key)[:max(1, min(int(args.get("adet") or 5), 8))]
        return {"araclar": [_v(x) for x in res], "katalog_buyuklugu": len(cat)}
    if name == "arac_karsilastir":
        vs = [v for v in (_find(cat, i) for i in (args.get("arac_idleri") or [])[:3]) if v]
        return {"araclar": [_v(x) for x in vs]} if vs else {"hata": "Araç bulunamadı; önce arac_ara kullan"}
    if name == "yolculuk_hesapla":
        v = _find(cat, args.get("arac_id")) or _find(cat, ctx.get("vehicleId"))
        if not v:
            return {"hata": "Araç bulunamadı"}
        f = SCENARIOS.get(args.get("senaryo") or "otoyol", 0.70)
        real = v["rangeWltp"] * f
        start = max(20, min(int(args.get("baslangic_yuzde") or 90), 100))
        first = real * (start - 10) / 100
        leg = real * 0.70
        km = max(1, int(args.get("mesafe_km") or 0))
        stops = 0 if km <= first else math.ceil((km - first) / leg)
        return {"arac": _v(v)["ad"], "senaryo_menzil_km": int(real), "ilk_bacak_km": int(first),
                "sarj_duragi": stops, "toplam_sarj_dk": stops * v["dc10to80Min"],
                "varsayim": f"%{start} ile çıkış, %10'da şarj, %10→%80 dolum"}
    if name == "maliyet_hesapla":
        v = _find(cat, args.get("arac_id")) or _find(cat, ctx.get("vehicleId"))
        cons = (v["batteryKwh"] / v["rangeWltp"] * 100 * 1.15) if v else 18.0
        price = args.get("kwh_fiyat_tl") or _avg_dc_price()
        km = max(1, int(args.get("mesafe_km") or 0))
        kwh = km * cons / 100
        return {"kwh": round(kwh, 1), "tuketim_kwh_100km": round(cons, 1),
                "kwh_fiyat_tl": price, "tutar_tl": round(kwh * price) if price else None,
                "not": None if price else "Güncel kWh fiyatı tanımlı değil; kullanıcıya fiyatı sor"}
    return {"hata": "Bilinmeyen fonksiyon"}


async def chat(device: str, messages: list, ctx: dict,
               stations_fn: Callable[..., Awaitable[dict]]) -> dict:
    if not config.GEMINI_API_KEY:
        return {"reply": "Asistan şu an kullanılamıyor.", "available": False}
    day = time.strftime("%Y%m%d", time.gmtime())
    used = await cache.incr(f"agent:d:{device}:{day}", 90000)
    if used > config.AGENT_DAILY_LIMIT:
        return {"reply": f"Bugünkü asistan mesaj hakkın doldu ({config.AGENT_DAILY_LIMIT}). Yarın tekrar görüşelim!",
                "available": True, "limited": True, "remaining": 0}

    cat = news._catalog()
    my = _find(cat, ctx.get("vehicleId"))
    ctx_text = (f'kullanıcının aracı: {_v(my)["ad"]} (id {my["id"]})' if my else "kullanıcı araç seçmemiş") + \
               ("; konum var" if ctx.get("lat") is not None else "; konum yok")
    contents = []
    for m in messages[-12:]:
        role = "model" if m.get("role") == "assistant" else "user"
        text = str(m.get("text") or "")[:config.AGENT_MAX_CHARS]
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})
    if not contents or contents[-1]["role"] != "user":
        return {"reply": "Bir soru yazar mısın?", "available": True}

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    used_tools = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=5.0)) as client:
        for _ in range(5):
            body = {"systemInstruction": {"parts": [{"text": SYSTEM.format(ctx=ctx_text)}]},
                    "contents": contents, "tools": TOOLS,
                    "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1024,
                                         "thinkingConfig": {"thinkingBudget": 0}}}
            try:
                r = await client.post(url, params={"key": config.GEMINI_API_KEY}, json=body)
            except httpx.HTTPError as e:
                log.warning("Gemini bağlantı: %s", e)
                return {"reply": "Asistana şu an ulaşılamıyor, birazdan tekrar dene.", "available": True}
            if r.status_code != 200:
                log.warning("Gemini HTTP %s: %s", r.status_code, r.text[:300])
                return {"reply": "Asistan şu an yanıt veremedi, birazdan tekrar dene.", "available": True}
            cand = (r.json().get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                text = "".join(p.get("text", "") for p in parts).strip()
                return {"reply": text or "Bunu yanıtlayamadım, farklı sorar mısın?", "available": True,
                        "tools": used_tools, "remaining": max(0, config.AGENT_DAILY_LIMIT - used)}
            contents.append({"role": "model", "parts": parts})
            responses = []
            for c in calls[:4]:
                name, args = c.get("name", ""), c.get("args") or {}
                used_tools.append(name)
                res = await _run_tool(name, args, ctx, stations_fn)
                responses.append({"functionResponse": {"name": name, "response": res}})
            contents.append({"role": "user", "parts": responses})
    return {"reply": "Bu soru biraz karışık oldu, daha kısa sorar mısın?", "available": True, "tools": used_tools}
