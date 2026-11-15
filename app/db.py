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
-- Google hesapları (device_id sütunları "sahip" olarak kullanılır: cihaz kimliği ya da "u:<id>")
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    google_sub  TEXT UNIQUE NOT NULL,
    email       TEXT NOT NULL,
    name        TEXT,
    picture     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    hash        TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Favoriler, ayarlar gibi küçük kullanıcı verileri
CREATE TABLE IF NOT EXISTS user_data (
    owner       TEXT NOT NULL,
    k           TEXT NOT NULL,
    data        JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, k)
);
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


_last_try = 0.0


async def ensure() -> bool:
    """Bağlantı yoksa (ör. API, Postgres'ten önce açıldıysa) en fazla 15 sn'de bir yeniden dener."""
    global _last_try
    if _pool is not None:
        return True
    import time
    if time.time() - _last_try < 15:
        return False
    _last_try = time.time()
    await connect()
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


# ── Kullanıcılar (Google girişi) ─────────────────────────────
import hashlib
import secrets
import uuid as _uuid


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def upsert_user(g: dict) -> dict:
    async with _pool.acquire() as c:
        row = await c.fetchrow("""INSERT INTO users (id, google_sub, email, name, picture) VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (google_sub) DO UPDATE SET email=EXCLUDED.email, name=EXCLUDED.name,
                picture=EXCLUDED.picture, last_login=now()
            RETURNING id, email, name, picture, created_at""",
            _uuid.uuid4().hex, g["sub"], g["email"], g["name"], g["picture"])
    return {"id": row["id"], "email": row["email"], "name": row["name"], "picture": row["picture"],
            "createdAt": int(row["created_at"].timestamp() * 1000)}


async def get_user(uid: str) -> Optional[dict]:
    async with _pool.acquire() as c:
        row = await c.fetchrow("SELECT id, email, name, picture, created_at FROM users WHERE id=$1", uid)
    if not row:
        return None
    return {"id": row["id"], "email": row["email"], "name": row["name"], "picture": row["picture"],
            "createdAt": int(row["created_at"].timestamp() * 1000)}


async def new_refresh(uid: str, device_id: str) -> str:
    token = secrets.token_urlsafe(32)
    async with _pool.acquire() as c:
        await c.execute("INSERT INTO refresh_tokens (hash, user_id, device_id) VALUES ($1,$2,$3)", _h(token), uid, device_id)
    return token


async def check_refresh(token: str, device_id: str) -> Optional[str]:
    """Yenileme anahtarı geçerli ve aynı cihaza aitse kullanıcı id'si."""
    async with _pool.acquire() as c:
        row = await c.fetchrow("""UPDATE refresh_tokens SET last_used=now() WHERE hash=$1 AND device_id=$2
                                  RETURNING user_id""", _h(token), device_id)
    return row["user_id"] if row else None


async def revoke_refresh(token: str) -> None:
    async with _pool.acquire() as c:
        await c.execute("DELETE FROM refresh_tokens WHERE hash=$1", _h(token))


async def migrate_device_to_user(device_id: str, uid: str) -> dict:
    """
    Girişte cihazdaki veriyi hesaba taşır.
    - Profil: hesapta yoksa cihazdakini taşır (varsa hesabınki kalır).
    - Şarj kayıtları: birleştirilir (aynı id tekrar eklenmez).
    - Favoriler vb.: hesapta yoksa taşınır.
    """
    owner = f"u:{uid}"
    moved = {"profile": False, "sessions": 0, "data": 0}
    async with _pool.acquire() as c:
        async with c.transaction():
            if not await c.fetchval("SELECT 1 FROM profiles WHERE device_id=$1", owner):
                r = await c.execute("UPDATE profiles SET device_id=$1 WHERE device_id=$2", owner, device_id)
                moved["profile"] = r.endswith("1")
            r = await c.execute("""INSERT INTO charge_sessions (device_id, id, data, charged_at)
                                   SELECT $1, id, data, charged_at FROM charge_sessions WHERE device_id=$2
                                   ON CONFLICT DO NOTHING""", owner, device_id)
            moved["sessions"] = int(r.split()[-1])
            await c.execute("DELETE FROM charge_sessions WHERE device_id=$1", device_id)
            r = await c.execute("""INSERT INTO user_data (owner, k, data, updated_at)
                                   SELECT $1, k, data, updated_at FROM user_data WHERE owner=$2
                                   ON CONFLICT DO NOTHING""", owner, device_id)
            moved["data"] = int(r.split()[-1])
            await c.execute("DELETE FROM user_data WHERE owner=$1", device_id)
            await c.execute("DELETE FROM profiles WHERE device_id=$1", device_id)
    return moved


async def delete_user(uid: str) -> None:
    """Hesap ve tüm verisi kalıcı olarak silinir (Play hesap silme şartı)."""
    owner = f"u:{uid}"
    async with _pool.acquire() as c:
        async with c.transaction():
            await c.execute("DELETE FROM profiles WHERE device_id=$1", owner)
            await c.execute("DELETE FROM charge_sessions WHERE device_id=$1", owner)
            await c.execute("DELETE FROM user_data WHERE owner=$1", owner)
            await c.execute("DELETE FROM users WHERE id=$1", uid)   # refresh_tokens CASCADE


async def get_data(owner: str, k: str) -> Optional[Any]:
    async with _pool.acquire() as c:
        row = await c.fetchrow("SELECT data FROM user_data WHERE owner=$1 AND k=$2", owner, k)
    return json.loads(row["data"]) if row else None


async def put_data(owner: str, k: str, data: Any) -> None:
    async with _pool.acquire() as c:
        await c.execute("""INSERT INTO user_data (owner, k, data, updated_at) VALUES ($1,$2,$3::jsonb, now())
                           ON CONFLICT (owner, k) DO UPDATE SET data=EXCLUDED.data, updated_at=now()""",
                        owner, k, json.dumps(data, ensure_ascii=False))
