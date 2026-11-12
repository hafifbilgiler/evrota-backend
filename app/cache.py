"""Redis önbelleği. Redis'e ulaşılamazsa servis önbelleksiz çalışmaya devam eder."""
import json
import logging
from typing import Any, Optional

import redis.asyncio as aioredis

from . import config

log = logging.getLogger("evrota.cache")
_client: Optional[aioredis.Redis] = None


async def connect() -> None:
    global _client
    try:
        _client = aioredis.from_url(config.REDIS_URL, decode_responses=True)
        await _client.ping()
        log.info("Redis bağlı: %s", config.REDIS_URL)
    except Exception as e:  # pragma: no cover - ortam bağımlı
        log.warning("Redis yok, önbelleksiz çalışılıyor: %s", e)
        _client = None


async def close() -> None:
    if _client is not None:
        await _client.close()


def client() -> Optional[aioredis.Redis]:
    return _client


async def get_json(key: str) -> Optional[Any]:
    if _client is None:
        return None
    try:
        raw = await _client.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def set_json(key: str, value: Any, ttl: int) -> None:
    if _client is None:
        return
    try:
        await _client.setex(key, ttl, json.dumps(value, ensure_ascii=False))
    except Exception:
        pass


async def incr(key: str, ttl: int) -> int:
    """Sayaç artırır; Redis yoksa 0 döner (sınır uygulanmaz)."""
    if _client is None:
        return 0
    try:
        pipe = _client.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl)
        value, _ = await pipe.execute()
        return int(value)
    except Exception:
        return 0


async def client_is_set() -> bool:
    return _client is not None


async def exists(key: str) -> bool:
    if _client is None:
        return False
    try:
        return bool(await _client.exists(key))
    except Exception:
        return False
