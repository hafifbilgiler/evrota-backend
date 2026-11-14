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


async def search_stations(lat: float, lng: float, radius: int, operator: Optional[str] = None) -> Optional[list]:
    """
    Yakındaki şarj istasyonları. Hata olursa None döner.
    operator verilirse: o operatörün istasyonları metin aramasıyla bulunur (yakındaki 20 istasyonla
    sınırlı kalmamak için), sonra operatöre göre süzülür.
    """
    if not config.GOOGLE_PLACES_API_KEY:
        log.error("GOOGLE_PLACES_API_KEY tanımlı değil")
        return None
    circle = {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(radius)}}
    if operator:
        url = "https://places.googleapis.com/v1/places:searchText"
        body = {"textQuery": f"{operator} şarj istasyonu", "includedType": "electric_vehicle_charging_station",
                "strictTypeFiltering": True, "pageSize": 20, "languageCode": "tr", "locationBias": circle}
    else:
        url = "https://places.googleapis.com/v1/places:searchNearby"
        body = {"includedTypes": ["electric_vehicle_charging_station"], "maxResultCount": 20,
                "rankPreference": "DISTANCE", "languageCode": "tr", "locationRestriction": circle}
    headers = {"X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY, "X-Goog-FieldMask": _FIELDS}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0)) as client:
            r = await client.post(url, json=body, headers=headers)
        if r.status_code != 200:
            log.warning("Places HTTP %s: %s", r.status_code, r.text[:300])
            return None
        out = [s for s in (normalize(p, lat, lng) for p in r.json().get("places", [])) if s]
        if operator:
            out = [s for s in out if s["operator"] == operator and s["distance"] <= radius * 1.5]
        out.sort(key=lambda x: x["distance"])
        log.info("Places → %d istasyon (%.4f,%.4f r=%d)", len(out), lat, lng, radius)
        return out
    except Exception as e:
        log.warning("Places hatası: %s", e)
        return None
