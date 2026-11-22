"""
Lansman kampanyası: ilk N (varsayılan 10) premium üyeye aracının markasına özel EVrota kupası.
Kurallar:
  - Google ile giriş yapmış hesap ("u:<id>"), doğrulanmış Google Play aboneliği
  - Test satın almaları sayılmaz (CAMPAIGN_ALLOW_TEST=true ile geçici olarak sayılabilir)
  - Sıra, aboneliğin sunucuda ilk doğrulandığı an; her hesaba bir hak, boşalan sıra yeniden dağıtılmaz
  - Kargo, deneme süresi bitip abonelik hâlâ aktifken (ilk ödeme alınmış) yapılır
Durumlar: reserved → address_given → ready (ödeme alındı) → shipped
"""
import json
import logging
import time
from typing import Optional

from . import config, db, news

log = logging.getLogger("evrota.campaign")
TRIAL_SECONDS = 7 * 86400

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign_entries (
    owner       TEXT PRIMARY KEY,
    slot        INT UNIQUE NOT NULL,
    brand       TEXT NOT NULL DEFAULT '',
    vehicle     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'reserved',
    address     JSONB,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    tracking    TEXT
);
"""


async def _ensure_schema():
    async with db._pool.acquire() as c:
        await c.execute(SCHEMA)


async def _vehicle_of(owner: str) -> tuple:
    prof = await db.get_profile(owner) or {}
    vid = prof.get("vehicleId")
    for v in news._catalog():
        if v.get("id") == vid:
            return v.get("brand", ""), f'{v.get("brand", "")} {v.get("model", "")}'.strip()
    return "", ""


async def remaining() -> int:
    await _ensure_schema()
    async with db._pool.acquire() as c:
        n = await c.fetchval("SELECT count(*) FROM campaign_entries")
    return max(0, config.CAMPAIGN_LIMIT - int(n))


async def try_reserve(owner: str, verified: dict) -> Optional[int]:
    """Premium doğrulanınca çağrılır. Uygunsa sıra numarası verir."""
    if not config.CAMPAIGN_LIMIT or not owner.startswith("u:") or not verified.get("active"):
        return None
    if verified.get("test") and not config.CAMPAIGN_ALLOW_TEST:
        return None
    await _ensure_schema()
    brand, vehicle = await _vehicle_of(owner)
    async with db._pool.acquire() as c:
        async with c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(424242)")   # aynı anda iki kişi aynı sırayı almasın
            if await c.fetchval("SELECT slot FROM campaign_entries WHERE owner=$1", owner):
                return None
            n = int(await c.fetchval("SELECT count(*) FROM campaign_entries"))
            if n >= config.CAMPAIGN_LIMIT:
                return None
            await c.execute("INSERT INTO campaign_entries (owner, slot, brand, vehicle) VALUES ($1, $2, $3, $4)",
                            owner, n + 1, brand, vehicle)
    log.info("Kampanya: %s %d. sırayı aldı (%s)", owner, n + 1, brand or "marka yok")
    return n + 1


async def entry(owner: str, premium_active: bool) -> Optional[dict]:
    await _ensure_schema()
    async with db._pool.acquire() as c:
        row = await c.fetchrow("""SELECT slot, brand, vehicle, status, address, tracking,
                                  extract(epoch FROM started_at)::bigint AS started FROM campaign_entries WHERE owner=$1""", owner)
    if not row:
        return None
    status = row["status"]
    # Deneme bitti ve abonelik hâlâ aktifse (ilk ödeme alındı) kargoya hazır
    if status == "address_given" and premium_active and time.time() - row["started"] > TRIAL_SECONDS:
        status = "ready"
        async with db._pool.acquire() as c:
            await c.execute("UPDATE campaign_entries SET status='ready', updated_at=now() WHERE owner=$1", owner)
    addr = row["address"]
    if isinstance(addr, str):
        addr = json.loads(addr)
    return {"slot": row["slot"], "brand": row["brand"], "vehicle": row["vehicle"], "status": status,
            "address": addr, "tracking": row["tracking"], "shipAfter": row["started"] + TRIAL_SECONDS}


async def set_address(owner: str, addr: dict, brand: str) -> bool:
    await _ensure_schema()
    async with db._pool.acquire() as c:
        r = await c.execute("""UPDATE campaign_entries SET address=$2::jsonb, brand=COALESCE(NULLIF($3, ''), brand),
                               status=CASE WHEN status IN ('reserved','address_given') THEN 'address_given' ELSE status END,
                               updated_at=now() WHERE owner=$1 AND status <> 'shipped'""",
                            owner, json.dumps(addr, ensure_ascii=False), brand)
    return r.endswith("1")


async def forget(owner: str) -> None:
    """Hesap silinince adres silinir; sıra numarası boşa düşmez (yeniden dağıtılmaz)."""
    try:
        await _ensure_schema()
        async with db._pool.acquire() as c:
            await c.execute("UPDATE campaign_entries SET address=NULL, status='deleted', owner='silindi:'||slot, "
                            "updated_at=now() WHERE owner=$1", owner)
    except Exception as e:
        log.warning("Kampanya kaydı silinemedi: %s", e)
