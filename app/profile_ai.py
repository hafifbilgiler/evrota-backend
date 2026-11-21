"""
AI profil analizi: GET /me/analysis
Kullanıcının profili (araç, ev elektrik fiyatı, günlük km, batarya sağlığı, tercihleri) ve
şarj kayıtlarından istatistik çıkarılır; Gemini bunları yorumlayıp somut öneriler yazar.
Sayılar sunucuda HESAPLANIR (model sayı uydurmaz, sadece yorumlar). Sonuç 12 saat önbellekte.
"""
import hashlib
import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from . import cache, config, news

log = logging.getLogger("evrota.profile_ai")
TR = timezone(timedelta(hours=3))


def stats(profile: dict, sessions: list) -> dict:
    now = datetime.now(TR)
    month_ago = (now - timedelta(days=30)).timestamp() * 1000
    last30 = [s for s in sessions if s.get("chargedAt", 0) >= month_ago]
    kwh = sum(s.get("kwh") or 0 for s in last30)
    cost = sum(s.get("cost") or 0 for s in last30)
    dc = [s for s in last30 if s.get("isDc")]
    hours = Counter(datetime.fromtimestamp(s["chargedAt"] / 1000, TR).hour for s in sessions if s.get("chargedAt"))
    stations = Counter(s.get("stationName") for s in sessions if s.get("stationName"))
    cat = {v["id"]: v for v in news._catalog()}
    v = cat.get(profile.get("vehicleId") or "")
    home = profile.get("homePriceKwh")
    out = {
        "kayit_sayisi_toplam": len(sessions),
        "son30gun": {"sarj": len(last30), "kwh": round(kwh, 1), "tutar_tl": round(cost),
                     "ort_tl_kwh": round(cost / kwh, 2) if kwh else None,
                     "dc_orani": round(len(dc) / len(last30), 2) if last30 else None,
                     "dc_ort_tl_kwh": round(sum(s["cost"] for s in dc) / max(0.1, sum(s["kwh"] for s in dc)), 2) if dc else None},
        "en_cok_saatler": [h for h, _ in hours.most_common(3)],
        "en_cok_istasyonlar": [n for n, _ in stations.most_common(3)],
        "arac": None if v is None else {"ad": f'{v["brand"]} {v["model"]}', "batarya_kwh": v["batteryKwh"],
                                        "wltp_km": v["rangeWltp"], "dc_kw": v["dcMaxKw"], "ac_kw": v["acMaxKw"]},
        "profil": {k: profile.get(k) for k in ("homePriceKwh", "hasHomeCharger", "homeChargerKw", "dailyKm",
                                               "batteryHealth", "preferredOperators", "climate", "usage")},
    }
    # Hesaplanmış tasarruf potansiyeli: DC şarjın evde yapılsaydı farkı
    if home and dc and profile.get("hasHomeCharger"):
        dc_kwh = sum(s["kwh"] for s in dc)
        out["hesap_evde_sarj_tasarrufu_tl"] = round(sum(s["cost"] for s in dc) - dc_kwh * float(home))
    if v and profile.get("dailyKm"):
        cons = v["batteryKwh"] / v["rangeWltp"] * 100 * 1.15
        out["hesap_aylik_tahmini_kwh"] = round(float(profile["dailyKm"]) * 30 * cons / 100)
        if home:
            out["hesap_aylik_ev_maliyeti_tl"] = round(out["hesap_aylik_tahmini_kwh"] * float(home))
    if v and profile.get("batteryHealth"):
        out["hesap_guncel_menzil_km"] = round(v["rangeWltp"] * float(profile["batteryHealth"]) / 100 * 0.85)
    return out


PROMPT = """Bir elektrikli araç sahibinin verilerini analiz et ve Türkçe, samimi, kısa öneriler yaz.

VERİ (sunucu hesapladı, sayılar doğru; YENİ SAYI UYDURMA, sadece bu sayıları kullan):
{data}

Kurallar:
- Sadece verideki sayıları kullan; veri yoksa o konuda yorum yapma.
- Kayıt azsa (5'ten az) bunu nazikçe söyle ve şarj kaydı eklemesini öner.
- Para tasarrufu, batarya sağlığı (ör. sürekli %100'e şarj, sık DC) ve uygun saat/istasyon önerilerine odaklan.
- Türkçe, net, pazarlama dili yok.

SADECE şu JSON'u döndür:
{{"ozet": "2 cümle",
  "bulgular": [{{"baslik": "kısa", "detay": "tek cümle, verideki sayıyla"}}],
  "oneriler": ["en fazla 4 somut öneri"],
  "tasarruf_tl": null}}
"tasarruf_tl": veride "hesap_evde_sarj_tasarrufu_tl" varsa o sayı, yoksa null."""


async def analyze(device: str, profile: dict, sessions: list, limit: Optional[int] = None) -> dict:
    data = stats(profile, sessions)
    key = "pa:" + hashlib.sha256((device + json.dumps(data, sort_keys=True)).encode()).hexdigest()[:32]
    cached = await cache.get_json(key)
    if cached:
        return {**cached, "cached": True}
    if not config.GEMINI_API_KEY:
        return {"available": False, "reason": "AI analizi şu an kullanılamıyor", "stats": data}
    day = time.strftime("%Y%m%d", time.gmtime())
    limit = limit or config.PROFILE_ANALYSIS_DAILY
    if await cache.incr(f"pa:d:{device}:{day}", 90000) > limit:
        return {"available": False, "reason": f"Bugünkü analiz hakkın doldu ({limit}). Yarın tekrar dene.",
                "stats": data}
    body = {"contents": [{"parts": [{"text": PROMPT.format(data=json.dumps(data, ensure_ascii=False, indent=1))}]}],
            "generationConfig": config.gemini_config(config.GEMINI_MODEL, 0.3, 2048, json_mode=True)}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=5.0)) as client:
            r = await client.post(url, headers={"x-goog-api-key": config.GEMINI_API_KEY}, json=body)
        if r.status_code != 200:
            log.warning("Gemini HTTP %s: %s", r.status_code, r.text[:300])
            return {"available": False, "reason": "AI analizi şu an hazırlanamadı", "stats": data}
        parts = r.json()["candidates"][0]["content"]["parts"]
        ai = json.loads("".join(p.get("text", "") for p in parts))
    except Exception as e:
        log.warning("Profil analizi hatası: %s", e)
        return {"available": False, "reason": "AI analizi şu an hazırlanamadı", "stats": data}
    res = {"available": True, "summary": str(ai.get("ozet", ""))[:400],
           "findings": [{"title": str(f.get("baslik", ""))[:60], "detail": str(f.get("detay", ""))[:200]}
                        for f in (ai.get("bulgular") or []) if isinstance(f, dict)][:5],
           "tips": [str(t)[:200] for t in (ai.get("oneriler") or [])][:4],
           "savingTl": data.get("hesap_evde_sarj_tasarrufu_tl"), "stats": data, "at": int(time.time())}
    await cache.set_json(key, res, 12 * 3600)
    return res
