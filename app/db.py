"""
PostgreSQL (kalıcı veri). DB_PASSWORD yoksa veritabanı kapalı kalır, API yine çalışır
(profil uçları 503 döner). Tablolar açılışta oluşturulur (IF NOT EXISTS).
"""
import json
import logging
from typing import Any, Optional

from . import config

log = logging.getLogger("evrota.db")
_pool = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    device_id   TEXT PRIMARY KEY,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS charge_sessions (
    device_id   TEXT NOT NULL,
    id          TEXT NOT NULL,
    data        JSONB NOT NULL,
    charged_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (device_id, id)
);
CREATE INDEX IF NOT EXISTS charge_sessions_dev_time ON charge_sessions (device_id, charged_at DESC);
"""


async def connect() -> None:
    global _pool
    if not config.DB_PASSWORD:
        log.warning("DB_PASSWORD yok: veritabanı kapalı (profil uçları çalışmaz)")
        return
    try:
        import asyncpg
        _pool = await asyncpg.create_pool(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                                          password=config.DB_PASSWORD, database=config.DB_NAME,
                                          min_size=1, max_size=5, command_timeout=10)
        async with _pool.acquire() as c:
            await c.execute(SCHEMA)
        log.info("PostgreSQL bağlı")
    except Exception as e:
        log.error("PostgreSQL bağlanamadı: %s", e)
        _pool = None


async def close() -> None:
    if _pool is not None:
        await _pool.close()


def ready() -> bool:
    return _pool is not None


async def get_profile(device: str) -> Optional[dict]:
    async with _pool.acquire() as c:
        row = await c.fetchrow("SELECT data, updated_at FROM profiles WHERE device_id=$1", device)
    if not row:
        return None
    return {**json.loads(row["data"]), "updatedAt": int(row["updated_at"].timestamp() * 1000)}


async def put_profile(device: str, data: dict) -> None:
    async with _pool.acquire() as c:
        await c.execute("""INSERT INTO profiles (device_id, data, updated_at) VALUES ($1, $2::jsonb, now())
                           ON CONFLICT (device_id) DO UPDATE SET data=EXCLUDED.data, updated_at=now()""",
                        device, json.dumps(data, ensure_ascii=False))


async def replace_sessions(device: str, sessions: list) -> int:
    """Telefondaki listenin tamamıyla değiştirir (silinenler de silinir)."""
    async with _pool.acquire() as c:
        async with c.transaction():
            await c.execute("DELETE FROM charge_sessions WHERE device_id=$1", device)
            if sessions:
                await c.executemany(
                    "INSERT INTO charge_sessions (device_id, id, data, charged_at) VALUES ($1,$2,$3::jsonb, to_timestamp($4/1000.0))",
                    [(device, s["id"], json.dumps(s, ensure_ascii=False), s["chargedAt"]) for s in sessions])
    return len(sessions)


async def get_sessions(device: str, limit: int = 500) -> list:
    async with _pool.acquire() as c:
        rows = await c.fetch("SELECT data FROM charge_sessions WHERE device_id=$1 ORDER BY charged_at DESC LIMIT $2",
                             device, limit)
    return [json.loads(r["data"]) for r in rows]
