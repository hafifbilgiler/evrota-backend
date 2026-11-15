"""
Rota planlayıcı: POST /route/plan

1) Google Routes API ile rota (mesafe, süre, çizgi)
2) Places "rota üzerinde arama" ile yol boyunca hızlı şarj istasyonları (uzun rotada parça parça)
3) Araca, mevsime (kış/yaz), otoyol tüketimine göre şarj durakları seçilir:
   - Her durakta varış %'si en az "rezerv" kalır
   - Sadece gerektiği kadar şarj edilir (en fazla %80, DC eğrisi sonrası yavaşlar)
   - Yüksek kW, araca uyan soket, anlık boşluk ve "genelde boş" geçmişi tercih edilir
Google çağrıları (rota + istasyonlar) aynı başlangıç/varış için 24 saat önbellekte.
"""
import hashlib
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from . import cache, config, news, occupancy, places

log = logging.getLogger("evrota.route")
TR = timezone(timedelta(hours=3))
_ST_FIELDS = ",".join(["places.id", "places.displayName", "places.formattedAddress", "places.location",
                       "places.evChargeOptions", "places.googleMapsUri", "nextPageToken"])


def decode_polyline(s: str) -> list:
    pts, i, lat, lng = [], 0, 0, 0
    while i < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63; i += 1
                result |= (b & 0x1f) << shift; shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += d
            else:
                lng += d
        pts.append((lat / 1e5, lng / 1e5))
    return pts


def encode_polyline(pts: list) -> str:
    out, plat, plng = [], 0, 0
    for lat, lng in pts:
        for v, p in ((round(lat * 1e5), plat), (round(lng * 1e5), plng)):
            d = v - p
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1f)) + 63)); d >>= 5
            out.append(chr(d + 63))
        plat, plng = round(lat * 1e5), round(lng * 1e5)
    return "".join(out)


def _km(a, b) -> float:
    return places.distance_m(a[0], a[1], b[0], b[1]) / 1000.0


def _cum(pts: list) -> list:
    c = [0.0]
    for i in range(1, len(pts)):
        c.append(c[-1] + _km(pts[i - 1], pts[i]))
    return c


def _project(pts: list, cum: list, p) -> tuple:
    """Noktanın rotadaki km'si ve rotaya uzaklığı (km). Basit: en yakın rota noktası."""
    best, bi = 1e9, 0
    step = max(1, len(pts) // 4000)
    for i in range(0, len(pts), step):
        d = _km(pts[i], p)
        if d < best:
            best, bi = d, i
    return cum[bi], best


async def _route(client, origin: dict, dest: dict) -> Optional[dict]:
    body = {"origin": origin, "destination": dest, "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_UNAWARE", "languageCode": "tr", "units": "METRIC"}
    r = await client.post("https://routes.googleapis.com/directions/v2:computeRoutes", json=body,
                          headers={"X-Goog-Api-Key": config.ROUTES_KEY,
                                   "X-Goog-FieldMask": "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline,routes.legs.endLocation"})
    if r.status_code != 200:
        log.warning("Routes HTTP %s: %s", r.status_code, r.text[:300])
        return None
    rs = r.json().get("routes") or []
    if not rs:
        return None
    x = rs[0]
    end = ((x.get("legs") or [{}])[-1].get("endLocation") or {}).get("latLng") or {}
    return {"distanceKm": round(x["distanceMeters"] / 1000, 1),
            "durationMin": round(int(str(x.get("duration", "0s")).rstrip("s") or 0) / 60),
            "polyline": x["polyline"]["encodedPolyline"],
            "end": [end.get("latitude"), end.get("longitude")]}


async def _stations_along(client, pts: list, cum: list, min_kw: int) -> list:
    """Uzun rotayı ~220 km'lik parçalara bölüp her parçada rota üzerinde arama yapar."""
    total = cum[-1]
    seg_len = 220.0
    found = {}
    start = 0.0
    while start < total - 20:
        end = min(total, start + seg_len)
        seg = [p for p, c in zip(pts, cum) if start <= c <= end]
        if len(seg) < 2:
            start = end; continue
        step = max(1, len(seg) // 600)        # çizgiyi sadeleştir
        seg = seg[::step] + [seg[-1]]
        body = {"textQuery": "elektrikli araç şarj istasyonu", "includedType": "electric_vehicle_charging_station",
                "pageSize": 20, "languageCode": "tr",
                "evOptions": {"minimumChargingRateKw": min_kw},
                "searchAlongRouteParameters": {"polyline": {"encodedPolyline": encode_polyline(seg)}}}
        r = await client.post("https://places.googleapis.com/v1/places:searchText", json=body,
                              headers={"X-Goog-Api-Key": config.ROUTES_KEY, "X-Goog-FieldMask": _ST_FIELDS})
        if r.status_code == 200:
            for p in r.json().get("places", []):
                st = places.normalize(p, pts[0][0], pts[0][1])
                if st:
                    found[st["id"]] = st
        else:
            log.warning("Rota üzeri arama HTTP %s: %s", r.status_code, r.text[:200])
        start = end
    out = []
    for st in found.values():
        rkm, off = _project(pts, cum, (st["lat"], st["lng"]))
        if off <= 3.0:
            out.append({**st, "routeKm": round(rkm, 1), "detourKm": round(off, 1)})
    out.sort(key=lambda s: s["routeKm"])
    return out


def _winter() -> bool:
    return datetime.now(TR).month in (11, 12, 1, 2, 3)


def _charge_minutes(v: dict, kw: int, from_pct: float, to_pct: float) -> int:
    """%10→80 süresinden orantılı; istasyon araçtan yavaşsa uzar; %80 üstü çok yavaş."""
    base = v["dc10to80Min"] / 70.0           # % başına dakika (araç tam güçte)
    speed = min(1.0, max(0.15, (kw or 50) / max(1, v["dcMaxKw"])))
    a, b = from_pct, min(to_pct, 80)
    m = max(0, b - a) * base / speed
    if to_pct > 80:
        m += (to_pct - 80) * base * 2.5 / speed
    return round(m)


async def plan(device: str, origin: dict, dest: dict, vehicle_id: Optional[str], start_soc: int,
               reserve: int = 10, min_kw: int = 50) -> dict:
    cat = {x["id"]: x for x in news._catalog()}
    v = cat.get(vehicle_id or "")
    if not v:
        return {"error": "Önce Araçlar sekmesinden aracını seç"}
    if not config.ROUTES_KEY:
        return {"error": "Rota planlayıcı şu an kullanılamıyor"}

    day = time.strftime("%Y%m%d", time.gmtime())
    key_src = json.dumps([origin, dest, min_kw], sort_keys=True, ensure_ascii=False)
    gkey = "rp:" + hashlib.sha256(key_src.encode()).hexdigest()[:32]
    g = await cache.get_json(gkey)
    if not g:
        if await cache.incr(f"rp:d:{device}:{day}", 90000) > config.ROUTE_DAILY_LIMIT:
            return {"error": f"Bugünkü rota hakkın doldu ({config.ROUTE_DAILY_LIMIT}). Yarın tekrar dene."}
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=5.0)) as client:
            route = await _route(client, origin, dest)
            if not route:
                return {"error": "Rota bulunamadı; varış yerini daha açık yaz (ör. 'Antalya Merkez')"}
            pts = decode_polyline(route["polyline"])
            cum = _cum(pts)
            stations = await _stations_along(client, pts, cum, min_kw) if route["distanceKm"] > 60 else []
        g = {"route": route, "stations": stations}
        await cache.set_json(gkey, g, 24 * 3600)

    route, stations = g["route"], g["stations"]
    winter = _winter()
    factor = 0.55 if winter else 0.70          # otoyol gerçek menzil / WLTP
    real_range = v["rangeWltp"] * factor
    km_per_pct = real_range / 100.0
    kwh_per_pct = v["batteryKwh"] / 100.0
    total = route["distanceKm"]

    pos, soc, stops, warnings = 0.0, float(start_soc), [], []
    for _ in range(12):
        reach = pos + (soc - reserve) * km_per_pct
        if reach >= total:
            break
        window = [s for s in stations if pos + 15 < s["routeKm"] <= reach]
        if not window:
            nxt = next((s for s in stations if s["routeKm"] > pos + 15), None)
            warnings.append(f"{round(pos)}. km'den sonra menzil içinde hızlı şarj istasyonu bulunamadı"
                            + (f"; en yakın istasyon {round(nxt['routeKm'])}. km'de" if nxt else ""))
            break
        far = reach - pos

        def score(s):
            prog = (s["routeKm"] - pos) / max(1, far)          # ne kadar ileride (verimlilik)
            kw = min(s["maxKw"], v["dcMaxKw"]) / max(50, v["dcMaxKw"])
            compat = 1.0 if any(c["type"] in ("CCS2", "Tesla", "NACS") for c in s["connectors"]) or not s["connectors"] else 0.0
            free = 0.15 if (s.get("availableCount") or 0) > 0 else 0.0
            return prog * 1.0 + kw * 0.6 + compat * 0.8 + free - s["detourKm"] * 0.05

        best = max(window, key=score)
        arrive = soc - (best["routeKm"] - pos) / km_per_pct
        need_to_dest = reserve + 5 + (total - best["routeKm"]) / km_per_pct
        target = min(80.0, max(arrive + 10, need_to_dest))
        mins = _charge_minutes(v, best["maxKw"], arrive, target)
        stops.append({
            "id": best["id"], "name": best["name"], "operator": best["operator"], "address": best["address"],
            "lat": best["lat"], "lng": best["lng"], "maxKw": best["maxKw"], "available": best.get("availableCount"),
            "total": best.get("totalCount"), "routeKm": best["routeKm"], "detourKm": best["detourKm"],
            "arriveSoc": round(arrive), "departSoc": round(target), "chargeMin": mins,
            "kwh": round((target - arrive) * kwh_per_pct, 1)})
        pos, soc = best["routeKm"], target

    arrive_dest = soc - (total - pos) / km_per_pct
    # Doluluk geçmişi (varsa): durak saatinde genelde ne kadar boş
    for s in stops:
        pat = await occupancy.pattern(s["id"])
        if pat and pat.get("enough"):
            s["usuallyFree"] = pat.get("now")
    price = None
    try:
        with open(config.PRICES_FILE, encoding="utf-8") as f:
            price = (json.load(f).get("charging") or {}).get("dcAvgTlKwh")
    except Exception:
        pass
    kwh_total = sum(s["kwh"] for s in stops)
    charge_total = sum(s["chargeMin"] for s in stops)
    return {
        "vehicle": f'{v["brand"]} {v["model"]}', "winter": winter, "realRangeKm": round(real_range),
        "distanceKm": total, "driveMin": route["durationMin"], "polyline": route["polyline"],
        "stops": stops, "chargeMin": charge_total, "totalMin": route["durationMin"] + charge_total,
        "arriveSoc": round(arrive_dest), "kwh": round(kwh_total, 1),
        "costTl": round(kwh_total * price) if isinstance(price, (int, float)) else None,
        "stationsFound": len(stations), "warnings": warnings,
        "assumptions": f"{'Kış' if winter else 'Yaz'} · otoyol · menzil ≈ {round(real_range)} km · rezerv %{reserve}",
    }
