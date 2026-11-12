"""
Cihaz kimlik doğrulaması (JWT)

Akış:
  1) Uygulama ilk açılışta POST /auth/device  {"deviceId": "<uuid>"}  gönderir.
  2) Sunucu imzalı bir access token döner (varsayılan 30 gün).
  3) Uygulama sonraki her istekte  Authorization: Bearer <token>  gönderir.
  4) Token süresi dolarsa / geçersizse 401 → uygulama yeniden /auth/device çağırır.

Koruma:
  - Token HS256 ile EVROTA_JWT_SECRET kullanılarak imzalanır; sahte token geçmez.
  - aud/iss kontrolü: başka bir servisin token'ı kabul edilmez.
  - IP başına saatlik token alma sınırı (token farmı engeli).
  - Token başına dakikalık/günlük istek sınırı (main.py).
  - Play Integrity: PLAY_INTEGRITY_REQUIRED=true yapılınca uygulamanın gerçek
    Play sürümü olduğu doğrulanmadan token verilmez (doğrulayıcı sonraki adımda).
"""
import re
import time
import uuid
from typing import Optional

import jwt
from fastapi import HTTPException, Request

from . import cache, config

ISSUER = "evrota-api"
AUDIENCE = "evrota-app"
_DEVICE_RE = re.compile(r"^[A-Za-z0-9\-]{16,64}$")


def issue_token(device_id: str) -> dict:
    now = int(time.time())
    exp = now + config.TOKEN_TTL_DAYS * 86400
    payload = {"sub": device_id, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": exp,
               "jti": uuid.uuid4().hex, "typ": "device"}
    token = jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")
    return {"token": token, "expiresAt": exp, "tokenType": "Bearer"}


def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"],
                          audience=AUDIENCE, issuer=ISSUER,
                          options={"require": ["exp", "iat", "sub", "jti"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Oturum süresi doldu", headers={"WWW-Authenticate": "Bearer"})
    except jwt.PyJWTError:
        raise HTTPException(401, "Geçersiz oturum", headers={"WWW-Authenticate": "Bearer"})


def client_ip(request: Request) -> str:
    # Gateway arkasında gerçek IP X-Forwarded-For'un ilk elemanıdır
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def register_device(device_id: str, request: Request, integrity_token: Optional[str]) -> dict:
    if not _DEVICE_RE.match(device_id or ""):
        raise HTTPException(400, "Geçersiz cihaz kimliği")
    if config.PLAY_INTEGRITY_REQUIRED:
        # TODO: Play Integrity token'ını Google'a doğrulat (decodeIntegrityToken)
        # ve packageName / appRecognitionVerdict / certificate digest kontrol et.
        if not integrity_token:
            raise HTTPException(403, "Uygulama doğrulanamadı")
        raise HTTPException(503, "Uygulama doğrulaması henüz yapılandırılmadı")
    ip = client_ip(request)
    n = await cache.incr(f"authip:{ip}:{int(time.time() // 3600)}", 3700)
    if n > config.TOKENS_PER_IP_HOUR:
        raise HTTPException(429, "Çok fazla deneme, biraz sonra tekrar deneyin")
    return issue_token(device_id)


async def require_device(request: Request) -> str:
    """Bearer token'ı doğrular, cihaz kimliğini döner ve token başına istek sınırı uygular."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Giriş gerekli", headers={"WWW-Authenticate": "Bearer"})
    claims = verify_token(header[7:].strip())
    device = claims["sub"]
    if await cache.client_is_set():
        if await cache.exists(f"revoked:{claims['jti']}") or await cache.exists(f"blockdev:{device}"):
            raise HTTPException(401, "Oturum iptal edildi", headers={"WWW-Authenticate": "Bearer"})
    minute = int(time.time() // 60)
    day = time.strftime("%Y%m%d", time.gmtime())
    if await cache.incr(f"rl:m:{device}:{minute}", 120) > config.REQUESTS_PER_MINUTE:
        raise HTTPException(429, "Çok hızlı istek gönderiyorsunuz")
    if await cache.incr(f"rl:d:{device}:{day}", 90000) > config.REQUESTS_PER_DAY:
        raise HTTPException(429, "Günlük kullanım sınırına ulaşıldı")
    request.state.device_id = device
    return device
