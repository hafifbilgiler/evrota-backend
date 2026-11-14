"""
Doluluk geçmişi ve tahmini — "Genelde ne zaman boş?"

Google her istasyon için sadece ANLIK boş soket sayısını verir; geçmiş vermez.
Biz her taze /stations sonucunu (en fazla 3 dakikada bir, hücre başına) saat dilimine göre
kaydederiz. Haftalar içinde her istasyon için "Salı 18:00'de genelde %30 boş" gibi bir
desen oluşur. Ek Google maliyeti yoktur (zaten çekilen veriden).

Redis: occ:{istasyon_id}  hash  alanlar  "{gün}:{saat}:a" (boş soket toplamı),
                                         "{gün}:{saat}:t" (toplam soket), "{gün}:{saat}:n" (örnek)
Aynı istasyon + aynı 15 dakika için tek örnek alınır (occ_seen anahtarı).
"""
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import cache

TR = timezone(timedelta(hours=3))
TTL = 90 * 86400
DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


async def record(stations: list) -> None:
    r = cache.client()
    if r is None:
        return
    now = datetime.now(TR)
    slot = f"{now.weekday()}:{now.hour}"
    q = int(time.time() // 900)
    try:
        pipe = r.pipeline()
        for s in stations:
            avail, total = s.get("availableCount"), s.get("totalCount") or 0
            if avail is None or total <= 0:
                continue
            if not await r.set(f"occ_seen:{s['id']}:{q}", 1, ex=1000, nx=True):
                continue
            k = f"occ:{s['id']}"
            pipe.hincrby(k, f"{slot}:a", int(avail))
            pipe.hincrby(k, f"{slot}:t", int(total))
            pipe.hincrby(k, f"{slot}:n", 1)
            pipe.expire(k, TTL)
        await pipe.execute()
    except Exception:
        pass   # tahmin verisi kritik değil


async def pattern(station_id: str) -> Optional[dict]:
    """7x24 boşluk oranı (0-1, veri yoksa null) + şimdiki saat ve gün için en boş saatler."""
    r = cache.client()
    if r is None:
        return None
    raw = await r.hgetall(f"occ:{station_id}")
    grid = [[None] * 24 for _ in range(7)]
    samples = 0
    for d in range(7):
        for h in range(24):
            a, t, n = (int(raw.get(f"{d}:{h}:{x}", 0)) for x in ("a", "t", "n"))
            if t > 0:
                grid[d][h] = round(a / t, 2)
                samples += n
    now = datetime.now(TR)
    today = grid[now.weekday()]
    known = [(h, v) for h, v in enumerate(today) if v is not None]
    best = sorted(known, key=lambda x: -x[1])[:3]
    return {"grid": grid, "samples": samples, "day": now.weekday(), "dayName": DAYS[now.weekday()],
            "hour": now.hour, "now": today[now.hour],
            "bestHours": [h for h, _ in best], "enough": samples >= 10}
