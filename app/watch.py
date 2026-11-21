"""
"Boşalınca haber ver": kullanıcı dolu bir istasyonu takibe alır; arka planda birkaç dakikada bir
Google'dan doluluk kontrol edilir, soket boşalınca takip edenlere bildirim gider (tek seferlik, 2 saat geçerli).

Redis:
  watch:stations            takip edilen istasyon kimlikleri (set)
  watch:{sid}               sahip → bitiş zamanı (sorted set)
  watch_meta:{sid}          istasyon adı/konumu (bildirim metni ve yol tarifi için)
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from . import cache, config, db, places, push

log = logging.getLogger("evrota.watch")


async def add(owner: str, sid: str, name: str, lat: float, lng: float) -> int:
    r = cache.client()
    if r is None:
        raise RuntimeError("redis")
    until = int(time.time()) + config.WATCH_TTL
    await r.zadd(f"watch:{sid}", {owner: until})
    await r.expire(f"watch:{sid}", config.WATCH_TTL + 600)
    await r.sadd("watch:stations", sid)
    await r.set(f"watch_meta:{sid}", json.dumps({"name": name, "lat": lat, "lng": lng}), ex=config.WATCH_TTL + 600)
    await r.sadd(f"watch_owner:{owner}", sid)
    await r.expire(f"watch_owner:{owner}", config.WATCH_TTL + 600)
    return until


async def remove(owner: str, sid: str) -> None:
    r = cache.client()
    if r is None:
        return
    await r.zrem(f"watch:{sid}", owner)
    await r.srem(f"watch_owner:{owner}", sid)


async def active_count(owner: str) -> int:
    r = cache.client()
    if r is None:
        return 0
    now, n = time.time(), 0
    for sid in await r.smembers(f"watch_owner:{owner}"):
        score = await r.zscore(f"watch:{sid}", owner)
        if score and score > now:
            n += 1
        else:
            await r.srem(f"watch_owner:{owner}", sid)
    return n


async def _check_once() -> None:
    r = cache.client()
    if r is None or not push.enabled() or not await db.ensure():
        return
    now = time.time()
    for sid in await r.smembers("watch:stations"):
        await r.zremrangebyscore(f"watch:{sid}", "-inf", now)
        owners = await r.zrange(f"watch:{sid}", 0, -1)
        if not owners:
            await r.srem("watch:stations", sid)
            continue
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        if await cache.incr(f"watchcap:{day}", 90000) > config.WATCH_CHECKS_DAILY:
            log.warning("Günlük takip kontrol sınırı aşıldı")
            return
        st = await places.get_station(sid)
        if not st or not st.get("availableCount"):
            continue
        meta = json.loads(await r.get(f"watch_meta:{sid}") or "{}")
        name = st.get("name") or meta.get("name") or "İstasyon"
        n = st["availableCount"]
        tokens = await db.push_tokens_for(owners, "stationFree")
        invalid = await push.send(tokens, f"{name}: boş soket var",
                                  f"Şu an {n} soket boş. Dokun, yol tarifini aç.",
                                  {"type": "station_free", "stationId": sid, "lat": st["lat"], "lng": st["lng"]})
        if invalid:
            await db.delete_push_tokens(invalid)
        log.info("Takip bildirimi: %s → %d kişi, %d anahtar", sid, len(owners), len(tokens))
        await r.delete(f"watch:{sid}", f"watch_meta:{sid}")
        await r.srem("watch:stations", sid)
        for o in owners:
            await r.srem(f"watch_owner:{o}", sid)


def _prices():
    try:
        with open(config.PRICES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


async def loop() -> None:
    """Uygulama ömrü boyunca çalışır. Birden fazla pod olursa Redis kilidi tek kontrol yapılmasını sağlar."""
    await asyncio.sleep(20)
    while True:
        try:
            r = cache.client()
            if r is not None and await r.set("watch:lock", "1", nx=True, ex=config.WATCH_INTERVAL - 10):
                await _check_once()
            if r is not None and await r.set("alerts:lock", "1", nx=True, ex=config.ALERT_INTERVAL - 10):
                from . import alerts
                await alerts.check_stations()
                await alerts.check_prices(_prices())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Takip kontrolü hatası: %s", e)
        await asyncio.sleep(config.WATCH_INTERVAL)
