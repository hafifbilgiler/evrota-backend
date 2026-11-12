"""
AI araç değerlendirmesi: GET /vehicles/{id}/insight
Katalogdaki GERÇEK teknik verilere ve benzer araçlara dayanarak Gemini ile
artılar / eksiler / kime uygun / rakiplere göre fark üretir. Sonuç 30 gün
önbellekte tutulur (her araç için tek Gemini çağrısı).
"""
import json
import logging
from typing import Optional

import httpx

from . import cache, config, news

log = logging.getLogger("evrota.insight")
TTL = 30 * 86400


def _rivals(v: dict, catalog: list, n: int = 4) -> list:
    """Aynı kasa tipinde, bataryası en yakın araçlar."""
    pool = [x for x in catalog if x["id"] != v["id"] and x.get("body") == v.get("body")] or \
           [x for x in catalog if x["id"] != v["id"]]
    return sorted(pool, key=lambda x: abs(x["batteryKwh"] - v["batteryKwh"]))[:n]


def _spec(x: dict) -> str:
    cons = x["batteryKwh"] / x["rangeWltp"] * 100
    return (f'{x["brand"]} {x["model"]} ({x.get("body","")}): batarya {x["batteryKwh"]} kWh, WLTP menzil '
            f'{x["rangeWltp"]} km, tüketim ~{cons:.1f} kWh/100km, DC {x["dcMaxKw"]} kW, %10-80 {x["dc10to80Min"]} dk, '
            f'AC {x["acMaxKw"]} kW')


PROMPT = """Türkiye'deki elektrikli araç kullanıcıları için tarafsız bir araç değerlendirmesi yaz.

Değerlendirilen araç:
{car}

Benzer araçlar (karşılaştırma için):
{rivals}

Kurallar:
- SADECE yukarıdaki teknik verilere dayan. Fiyat, donanım, güvenlik puanı gibi verilmeyen bilgileri yazma.
- Tarafsız ol; artıları ve eksileri dengeli yaz. Pazarlama dili kullanma.
- Gerçek kullanımda menzilin WLTP'den düşük olduğunu (yazın ~%85, kışın otoyolda ~%60) dikkate alabilirsin.
- Türkçe, kısa ve anlaşılır yaz.

SADECE şu JSON'u döndür:
{{"ozet": "2 cümlelik genel değerlendirme",
  "artilar": ["en fazla 3 madde"],
  "eksiler": ["en fazla 3 madde"],
  "kime_uygun": "tek cümle (ör. günlük şehir içi kullanım ve ara sıra şehirlerarası yol)",
  "rakipler": [{{"id": "rakip araç id", "fark": "bu araca göre tek cümlelik fark"}}]}}

Rakip id'leri: {rival_ids}"""


async def get_insight(vehicle_id: str) -> Optional[dict]:
    catalog = news._catalog()
    v = next((x for x in catalog if x["id"] == vehicle_id), None)
    if v is None:
        return None
    key = f"insight:v1:{vehicle_id}"
    cached = await cache.get_json(key)
    if cached:
        return cached
    if not config.GEMINI_API_KEY:
        return {"available": False, "reason": "AI değerlendirmesi şu an kullanılamıyor"}
    rivals = _rivals(v, catalog)
    prompt = PROMPT.format(car=_spec(v), rivals="\n".join("- " + _spec(x) for x in rivals),
                           rival_ids=", ".join(x["id"] for x in rivals))
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json", "maxOutputTokens": 4096}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=5.0)) as client:
            r = await client.post(url, params={"key": config.GEMINI_API_KEY}, json=body)
        if r.status_code != 200:
            log.warning("Gemini HTTP %s: %s", r.status_code, r.text[:200])
            return {"available": False, "reason": "AI değerlendirmesi şu an hazırlanamadı"}
        data = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as e:
        log.warning("Insight hatası: %s", e)
        return {"available": False, "reason": "AI değerlendirmesi şu an hazırlanamadı"}
    ids = {x["id"]: x for x in rivals}
    result = {
        "available": True, "vehicleId": vehicle_id, "aiGenerated": True,
        "summary": str(data.get("ozet", ""))[:400],
        "pros": [str(x)[:140] for x in data.get("artilar", [])][:3],
        "cons": [str(x)[:140] for x in data.get("eksiler", [])][:3],
        "bestFor": str(data.get("kime_uygun", ""))[:200],
        "rivals": [{"id": x["id"], "name": f'{ids[x["id"]]["brand"]} {ids[x["id"]]["model"]}', "diff": str(x.get("fark", ""))[:160]}
                   for x in data.get("rakipler", []) if isinstance(x, dict) and x.get("id") in ids][:4],
    }
    await cache.set_json(key, result, TTL)
    return result
