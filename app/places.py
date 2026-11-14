"""Google Places API (New) — şarj istasyonları ve anlık doluluk."""
import logging
import math
import re
from datetime import datetime
from typing import Optional

import httpx

from . import config

log = logging.getLogger("evrota.places")

_FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.location",
    "places.evChargeOptions", "places.googleMapsUri", "places.rating", "places.userRatingCount",
])
_CONNECTOR = {
    "EV_CONNECTOR_TYPE_CCS_COMBO_2": "CCS2", "EV_CONNECTOR_TYPE_CCS_COMBO_1": "CCS1",
    "EV_CONNECTOR_TYPE_TYPE_2": "Type 2", "EV_CONNECTOR_TYPE_J1772": "Type 1",
    "EV_CONNECTOR_TYPE_CHADEMO": "CHAdeMO", "EV_CONNECTOR_TYPE_TESLA": "Tesla",
    "EV_CONNECTOR_TYPE_NACS": "NACS", "EV_CONNECTOR_TYPE_UNSPECIFIED_GB_T": "GB/T",
    "EV_CONNECTOR_TYPE_UNSPECIFIED_WALL_OUTLET": "Priz", "EV_CONNECTOR_TYPE_OTHER": "Diğer",
}


# Operatör adı normalleştirme: Google'daki istasyon adından operatörü çıkarır
OPERATORS = [
    ("ZES", r"\bzes\b"), ("Eşarj", r"e[şs]arj"), ("Trugo", r"trugo"), ("Voltrun", r"voltrun"),
    ("Sharz", r"sharz"), ("Tesla", r"tesla|supercharger"), ("Shell Recharge", r"shell"),
    ("Opet", r"\bopet\b"), ("Petrol Ofisi", r"petrol ofisi|\bpo\b"), ("Aksa", r"aksa"),
    ("Astor", r"astor"), ("Beefull", r"beefull"), ("WAT Mobilite", r"\bwat\b"), ("G-charge", r"g-?charge"),
    ("EVBox", r"evbox"), ("Otopriz", r"otopriz"), ("Enyakıt", r"enyak[ıi]t"), ("Ionity", r"ionity"),
    ("Togg Trugo", r"togg"), ("Kalyon", r"kalyon|oncharge"), ("Mypower", r"mypower"),
]
_OP_RE = [(name, re.compile(rx, re.I)) for name, rx in OPERATORS]
OPERATOR_NAMES = sorted({n for n, _ in OPERATORS if n != "Togg Trugo"})


def operator_of(name: str) -> str:
    for op, rx in _OP_RE:
        if rx.search(name or ""):
            return "Trugo" if op == "Togg Trugo" else op
    return "Diğer"


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(2 * r * math.asin(math.sqrt(a)))


def _epoch(ts: Optional[str]) -> Optional[int]:
    if not ts:
        return None
    try:
        clean = re.sub(r"\.\d+", "", ts).replace("Z", "+00:00")
        return int(datetime.fromisoformat(clean).timestamp())
    except Exception:
        return None


def normalize(place: dict, lat: float, lng: float) -> Optional[dict]:
    """Google cevabını uygulamanın beklediği biçime çevirir (EvStation)."""
    loc = place.get("location") or {}
    if "latitude" not in loc:
        return None
    connectors, total, avail, has_avail, max_kw, updated = [], 0, 0, False, 0.0, None
    for a in (place.get("evChargeOptions") or {}).get("connectorAggregation", []):
        cnt = int(a.get("count", 0) or 0)
        kw = float(a.get("maxChargeRateKw", 0) or 0)
        item = {"type": _CONNECTOR.get(a.get("type", ""), "Diğer"), "kw": round(kw),
                "count": cnt, "available": None, "outOfService": None}
        if "availableCount" in a or "outOfServiceCount" in a:
            has_avail = True
            item["available"] = int(a.get("availableCount", 0) or 0)
            item["outOfService"] = int(a.get("outOfServiceCount", 0) or 0)
            avail += item["available"]
            up = _epoch(a.get("availabilityLastUpdateTime"))
            if up and (updated is None or up > updated):
                updated = up
        total += cnt
        max_kw = max(max_kw, kw)
        connectors.append(item)
    return {
        "id": place.get("id", ""),
        "name": (place.get("displayName") or {}).get("text") or "Şarj istasyonu",
        "operator": operator_of((place.get("displayName") or {}).get("text") or ""),
        "address": place.get("formattedAddress", ""),
        "lat": loc["latitude"], "lng": loc["longitude"],
        "distance": distance_m(lat, lng, loc["latitude"], loc["longitude"]),
        "connectors": connectors,
        "totalCount": total,
        "availableCount": avail if has_avail else None,
        "maxKw": round(max_kw),
        "updated": updated,
        "rating": place.get("rating"),
        "ratingCount": place.get("userRatingCount", 0),
        "mapsUri": place.get("googleMapsUri"),
    }


async def _post(client: httpx.AsyncClient, url: str, body: dict, fields: str) -> Optional[dict]:
    headers = {"X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY, "X-Goog-FieldMask": fields}
    r = await client.post(url, json=body, headers=headers)
    if r.status_code != 200:
        log.warning("Places HTTP %s: %s", r.status_code, r.text[:300])
        return None
    return r.json()


def _rect(lat: float, lng: float, radius: int) -> dict:
    """Metin araması daire kabul etmez; yarıçapı kapsayan dikdörtgen."""
    dlat = radius / 111320.0
    dlng = radius / (111320.0 * max(0.2, math.cos(math.radians(lat))))
    return {"rectangle": {"low": {"latitude": lat - dlat, "longitude": lng - dlng},
                          "high": {"latitude": lat + dlat, "longitude": lng + dlng}}}


async def _text_pages(client, query: str, lat: float, lng: float, radius: int, pages: int):
    """searchText ile sayfa sayfa (sayfa başı 20, en fazla 3 sayfa = 60) istasyon. (yerler, kalan_var, çağrı)"""
    url = "https://places.googleapis.com/v1/places:searchText"
    body = {"textQuery": query, "includedType": "electric_vehicle_charging_station",
            "strictTypeFiltering": True, "pageSize": 20, "languageCode": "tr",
            "locationRestriction": _rect(lat, lng, radius)}
    found, token, calls = [], None, 0
    for _ in range(pages):
        if token:
            body["pageToken"] = token
        data = await _post(client, url, body, _FIELDS + ",nextPageToken")
        calls += 1
        if data is None:
            break
        found += data.get("places", [])
        token = data.get("nextPageToken")
        if not token:
            break
    return found, bool(token), calls


async def search_stations(lat: float, lng: float, radius: int, operator: Optional[str] = None) -> Optional[dict]:
    """
    Yakındaki şarj istasyonları. Hata olursa None döner.
    Dönüş: {"stations": [...], "capped": bool (daha fazlası olabilir), "calls": Google çağrı sayısı}

    - Firma yoksa: önce yakınlık sıralı 20 istasyon (searchNearby). 20'nin tamamı dolduysa ve yarıçap
      genişse metin aramasıyla sayfa sayfa 60'a kadar genişletilir; böylece firma sayıları da doğru çıkar.
    - Firma varsa: "<firma> şarj istasyonu" metin araması, 60'a kadar, sonra firmaya göre süzülür.
    """
    if not config.GOOGLE_PLACES_API_KEY:
        log.error("GOOGLE_PLACES_API_KEY tanımlı değil")
        return None
    circle = {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(radius)}}
    calls, capped, raw = 0, False, []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0)) as client:
            if operator:
                raw, capped, calls = await _text_pages(client, f"{operator} şarj istasyonu", lat, lng, radius, 3)
            else:
                body = {"includedTypes": ["electric_vehicle_charging_station"], "maxResultCount": 20,
                        "rankPreference": "DISTANCE", "languageCode": "tr", "locationRestriction": circle}
                data = await _post(client, "https://places.googleapis.com/v1/places:searchNearby", body, _FIELDS)
                calls += 1
                if data is None:
                    return None
                raw = data.get("places", [])
                if len(raw) >= 20:
                    if radius > 3000:
                        more, capped, c = await _text_pages(client, "elektrikli araç şarj istasyonu", lat, lng, radius, 3)
                        calls += c
                        raw += more
                    else:
                        capped = True
        seen, out = set(), []
        for p in raw:
            st = normalize(p, lat, lng)
            if not st or st["id"] in seen or st["distance"] > radius:
                continue
            if operator and st["operator"] != operator:
                continue
            seen.add(st["id"])
            out.append(st)
        out.sort(key=lambda x: x["distance"])
        if not operator and len(out) >= 60:
            capped = True
        log.info("Places → %d istasyon (%.4f,%.4f r=%d op=%s, %d çağrı)", len(out), lat, lng, radius, operator, calls)
        return {"stations": out, "capped": capped, "calls": calls}
    except Exception as e:
        log.warning("Places hatası: %s", e)
        return None
