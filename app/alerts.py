"""
Premium alarmları:
  - Favori istasyon alarmı: seçilen (en fazla 5) favori istasyon doludan boşa geçince ya da
    sürücüler arızalı bildirince bildirim.
  - Fiyat alarmı: prices.json'daki operatör şarj fiyatları değişince bildirim.
Ayarlar user_data'da "alerts" anahtarında: {"stations": [{id, name, lat, lng}], "price": bool}
Kontroller watch.py ile aynı döngüde, Redis kilidiyle tek pod'da çalışır.
"""
import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from . import cache, config, db, places, premium, push

log = logging.getLogger("evrota.alerts")
MAX_STATIONS = 5


async def _owners_with_alerts() -> list:
    async with db._pool.acquire() as c:
        rows = await c.fetch("SELECT owner, data FROM user_data WHERE k='alerts'")
    out = []
    for r in rows:
        try:
            out.append((r["owner"], json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]))
        except Exception:
            continue
    return out


async def _station_now(sid: str):
    """İstasyonun güncel durumu (10 dk önbellek: aynı istasyonu birden çok kişi izlese de tek sorgu)."""
    key = f"alst:{sid}"
    hit = await cache.get_json(key)
    if hit is not None:
        return hit
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    if await cache.incr(f"watchcap:{day}", 90000) > config.WATCH_CHECKS_DAILY:
        return None
    st = await places.get_station(sid)
    if st:
        await cache.set_json(key, {"available": st.get("availableCount"), "name": st.get("name"),
                                   "lat": st.get("lat"), "lng": st.get("lng")}, 600)
        return await cache.get_json(key)
    return None


async def _last_report(sid: str):
    r = cache.client()
    try:
        items = await r.zrevrange(f"rep:{sid}", 0, 0, withscores=True)
        if items and items[0][1] > time.time() - config.REPORT_TTL:
            return json.loads(items[0][0]).get("type"), int(items[0][1])
    except Exception:
        pass
    return None, None


async def check_stations() -> None:
    r = cache.client()
    if r is None or not push.enabled() or not await db.ensure():
        return
    for owner, cfg in await _owners_with_alerts():
        stations = (cfg or {}).get("stations") or []
        if not stations or not await premium.is_premium(owner):
            continue
        for s in stations[:MAX_STATIONS]:
            sid = s.get("id")
            if not sid:
                continue
            now = await _station_now(sid)
            if not now:
                continue
            name = now.get("name") or s.get("name") or "Favori istasyonun"
            avail = now.get("available")
            prev_key = f"alprev:{owner}:{sid}"
            prev = await r.get(prev_key)
            await r.set(prev_key, "" if avail is None else str(avail), ex=86400 * 3)
            data = {"type": "favorite", "stationId": sid, "lat": now.get("lat") or s.get("lat"),
                    "lng": now.get("lng") or s.get("lng")}
            if avail is not None and prev == "0" and avail > 0:
                await _send(owner, f"{name}: boş soket var", f"Favori istasyonunda şu an {avail} soket boş.", data)
            # Sürücü bildirimi: arızalı / kapalı (aynı bildirim bir kez gider)
            rtype, rts = await _last_report(sid)
            if rtype in ("Arizali", "Kapali") and rts:
                seen = f"alrep:{owner}:{sid}:{rts}"
                if await r.set(seen, "1", nx=True, ex=config.REPORT_TTL):
                    word = "arızalı" if rtype == "Arizali" else "kapalı"
                    await _send(owner, f"{name}: {word} bildirildi",
                                f"Bir sürücü favori istasyonunu az önce {word} olarak bildirdi.", data)


def _operators(prices: dict) -> dict:
    out = {}
    for o in ((prices or {}).get("charging") or {}).get("operators") or []:
        n = o.get("name") or o.get("operator")
        if n:
            out[n] = o
    return out


async def check_prices(prices: dict) -> None:
    """prices.json değiştiyse fiyat alarmı açık premium kullanıcılara bildirim."""
    r = cache.client()
    if r is None or not prices or not push.enabled() or not await db.ensure():
        return
    ops = _operators(prices)
    snap = hashlib.sha256(json.dumps(ops, sort_keys=True).encode()).hexdigest()
    old_raw = await r.get("alprice:snap")
    if old_raw is None:
        await r.set("alprice:snap", snap)
        await r.set("alprice:ops", json.dumps(ops))
        return
    if old_raw == snap:
        return
    old_ops = json.loads(await r.get("alprice:ops") or "{}")
    await r.set("alprice:snap", snap)
    await r.set("alprice:ops", json.dumps(ops))
    changed = []
    for name, o in ops.items():
        p = old_ops.get(name)
        if p is None:
            continue
        for k, label in (("dcTlKwh", "DC"), ("acTlKwh", "AC")):
            a, b = p.get(k), o.get(k)
            if a and b and a != b:
                changed.append(f"{name} {label} {str(a).replace('.', ',')} → {str(b).replace('.', ',')} TL/kWh")
    if not changed:
        return
    body = "; ".join(changed[:3]) + (" ve diğerleri" if len(changed) > 3 else "")
    for owner, cfg in await _owners_with_alerts():
        if (cfg or {}).get("price") and await premium.is_premium(owner):
            await _send(owner, "Şarj fiyatları değişti", body, {"type": "price"}, pref="price")


async def _send(owner: str, title: str, body: str, data: dict, pref: str = "favorites") -> None:
    tokens = await db.push_tokens_for([owner], pref)
    invalid = await push.send(tokens, title, body, data)
    if invalid:
        await db.delete_push_tokens(invalid)
