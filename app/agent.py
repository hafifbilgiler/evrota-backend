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


class _ProviderDown(Exception):
    """Sağlayıcı kullanılamıyor (kredi bitti, anahtar yok, bağlantı yok) → yedeğe geç."""


async def _gemini(client, system: str, history: list, ctx: dict, stations_fn, used_tools: list) -> str:
    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["text"]}]}
                for m in history]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    for _ in range(5):
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents, "tools": TOOLS,
                "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1024,
                                     "thinkingConfig": {"thinkingBudget": 0}}}
        try:
            r = await client.post(url, headers={"x-goog-api-key": config.GEMINI_API_KEY}, json=body)
        except httpx.HTTPError as e:
            raise _ProviderDown(f"Gemini bağlantı: {e}")
        if r.status_code != 200:
            raise _ProviderDown(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
        cand = (r.json().get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        if not calls:
            return "".join(p.get("text", "") for p in parts).strip()
        contents.append({"role": "model", "parts": parts})
        responses = []
        for c in calls[:4]:
            name, args = c.get("name", ""), c.get("args") or {}
            used_tools.append(name)
            responses.append({"functionResponse": {"name": name, "response": await _run_tool(name, args, ctx, stations_fn)}})
        contents.append({"role": "user", "parts": responses})
    return ""


# OpenAI uyumlu biçim (DeepInfra vb.) için aynı fonksiyon tanımları
OPENAI_TOOLS = [{"type": "function", "function": f} for f in TOOLS[0]["functionDeclarations"]]


async def _openai_compat(client, system: str, history: list, ctx: dict, stations_fn, used_tools: list) -> str:
    """DeepInfra (OpenAI uyumlu) — function calling ile aynı EVrota araçlarını kullanır."""
    msgs = [{"role": "system", "content": system}] + \
           [{"role": m["role"], "content": m["text"]} for m in history]
    url = config.DEEPINFRA_BASE_URL.rstrip("/") + "/chat/completions"
    for _ in range(5):
        body = {"model": config.DEEPINFRA_MODEL, "messages": msgs, "tools": OPENAI_TOOLS,
                "tool_choice": "auto", "temperature": 0.4, "max_tokens": 1024}
        try:
            r = await client.post(url, headers={"Authorization": f"Bearer {config.DEEPINFRA_API_KEY}"}, json=body)
        except httpx.HTTPError as e:
            raise _ProviderDown(f"DeepInfra bağlantı: {e}")
        if r.status_code != 200:
            raise _ProviderDown(f"DeepInfra HTTP {r.status_code}: {r.text[:300]}")
        msg = ((r.json().get("choices") or [{}])[0]).get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            text = (msg.get("content") or "").strip()
            # Bazı modeller düşünce bloğu döndürür; kullanıcıya göstermeyiz
            if "</think>" in text:
                text = text.split("</think>", 1)[1].strip()
            return text
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls[:4]:
            fn = c.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            used_tools.append(name)
            res = await _run_tool(name, args, ctx, stations_fn)
            msgs.append({"role": "tool", "tool_call_id": c.get("id", name), "content": json.dumps(res, ensure_ascii=False)})
    return ""


def _providers() -> list:
    """AGENT_PROVIDER sırası: ilk çalışan kullanılır, hata olursa sıradakine geçilir."""
    avail = {"gemini": (bool(config.GEMINI_API_KEY), _gemini),
             "deepinfra": (bool(config.DEEPINFRA_API_KEY), _openai_compat)}
    order = [p.strip() for p in config.AGENT_PROVIDER.split(",") if p.strip()]
    return [(name, avail[name][1]) for name in order if name in avail and avail[name][0]]


async def chat(device: str, messages: list, ctx: dict,
               stations_fn: Callable[..., Awaitable[dict]]) -> dict:
    providers = _providers()
    if not providers:
        return {"reply": "Asistan şu an kullanılamıyor.", "available": False}
    day = time.strftime("%Y%m%d", time.gmtime())
    used = await cache.incr(f"agent:d:{device}:{day}", 90000)   # (hata olsa da sayılır: kötüye kullanım koruması)
    if used > config.AGENT_DAILY_LIMIT:
        return {"reply": f"Bugünkü asistan mesaj hakkın doldu ({config.AGENT_DAILY_LIMIT}). Yarın tekrar görüşelim!",
                "available": True, "limited": True, "remaining": 0}

    cat = news._catalog()
    my = _find(cat, ctx.get("vehicleId"))
    ctx_text = (f'kullanıcının aracı: {_v(my)["ad"]} (id {my["id"]})' if my else "kullanıcı araç seçmemiş") + \
               ("; konum var" if ctx.get("lat") is not None else "; konum yok")
    history = []
    for m in messages[-12:]:
        text = str(m.get("text") or "")[:config.AGENT_MAX_CHARS]
        if text:
            history.append({"role": "assistant" if m.get("role") == "assistant" else "user", "text": text})
    if not history or history[-1]["role"] != "user":
        return {"reply": "Bir soru yazar mısın?", "available": True}

    system = SYSTEM.format(ctx=ctx_text)
    async with httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=5.0)) as client:
        for name, fn in providers:
            used_tools: list = []
            try:
                text = await fn(client, system, history, ctx, stations_fn, used_tools)
            except _ProviderDown as e:
                log.warning("Asistan sağlayıcısı %s kullanılamadı: %s", name, e)
                continue
            except Exception as e:
                log.exception("Asistan %s hatası: %s", name, e)
                continue
            return {"reply": text or "Bunu yanıtlayamadım, farklı sorar mısın?", "available": True,
                    "provider": name, "tools": used_tools,
                    "remaining": max(0, config.AGENT_DAILY_LIMIT - used)}
    return {"reply": "Asistan şu an bakımda, kısa süre içinde tekrar açılacak.", "available": False}
