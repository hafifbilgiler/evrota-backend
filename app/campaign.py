"""
Lansman kampanyası: ücretsiz denemeden sonra ücretli Premium'a geçen ilk N (varsayılan 10) üyeye
şarj temalı EVrota kupası
(kupada araç markası/logosu kullanılmaz; "brand" sütunu bilgi amaçlı araç markası ya da kullanıcının kupa notu).
Kurallar:
  - Google ile giriş yapmış hesap ("u:<id>"), Google Play'de doğrulanmış ve İLK ÖDEMESİ ALINMIŞ abonelik
    (deneme süresindeki üye sıra almaz)
  - Test satın almaları sayılmaz (CAMPAIGN_ALLOW_TEST=true ile geçici olarak sayılabilir)
  - Sıra, ilk ödemenin sunucuda görüldüğü an; her hesaba bir hak, boşalan sıra yeniden dağıtılmaz
  - Ödeme görülme anları: uygulama açılınca (GET /me/campaign, POST /me/premium) ve arka planda sweep()
Durumlar: reserved → address_given → ready (yönetici hazırlıyor) → shipped
"""
import json
import logging
import time
from typing import Optional

from . import config, db, news

log = logging.getLogger("evrota.campaign")

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
    if not config.CAMPAIGN_LIMIT or not owner.startswith("u:") or not verified.get("paid"):
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
    addr = row["address"]
    if isinstance(addr, str):
        addr = json.loads(addr)
    return {"slot": row["slot"], "brand": row["brand"], "vehicle": row["vehicle"], "status": status,
            "address": addr, "tracking": row["tracking"]}


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


async def check(owner: str) -> Optional[int]:
    """Uygulama kampanya kartını açınca: kontenjan varsa ve kaydı yoksa aboneliği Play'e yeniden sor
    (en fazla 10 dakikada bir), ilk ödeme alınmışsa sıra ver."""
    if not config.CAMPAIGN_LIMIT or not owner.startswith("u:"):
        return None
    from . import cache, premium
    r = cache.client()
    if r is not None and not await r.set(f"camp:chk:{owner}", "1", nx=True, ex=600):
        return None
    await _ensure_schema()
    async with db._pool.acquire() as c:
        if await c.fetchval("SELECT 1 FROM campaign_entries WHERE owner=$1", owner):
            return None
    if await remaining() <= 0:
        return None
    row = await db.latest_premium(owner)
    if not row:
        return None
    v = await premium.verify(row["purchase_token"])
    if not v:
        return None
    await db.put_premium(owner, row["purchase_token"], v["product"], v["state"], v["expires"], v["test"])
    if r is not None:
        await r.delete(f"prem:{owner}")
    return await try_reserve(owner, v)


async def sweep(limit: int = 20) -> None:
    """Arka plan: deneme süresi bitmiş (kayıtlı bitiş geçmiş) ve henüz sırası olmayan girişli üyeleri
    Play'e sorar; yenilenip ödeme alınmışsa sıra verir. Uygulamayı açmayan kazananlar da kaçmasın."""
    if not config.CAMPAIGN_LIMIT or not await db.ensure() or await remaining() <= 0:
        return
    from . import premium
    async with db._pool.acquire() as c:
        rows = await c.fetch("""SELECT DISTINCT ON (p.owner) p.owner, p.purchase_token FROM premium_subs p
                                WHERE p.owner LIKE 'u:%' AND p.expires_at < now() AND p.checked_at < now() - interval '30 minutes'
                                  AND p.expires_at > now() - interval '3 days'
                                  AND NOT EXISTS (SELECT 1 FROM campaign_entries e WHERE e.owner = p.owner)
                                ORDER BY p.owner, p.expires_at DESC LIMIT $1""", limit)
    for r in rows:
        v = await premium.verify(r["purchase_token"])
        if not v:
            continue
        await db.put_premium(r["owner"], r["purchase_token"], v["product"], v["state"], v["expires"], v["test"])
        if await try_reserve(r["owner"], v) is not None and await remaining() <= 0:
            return
