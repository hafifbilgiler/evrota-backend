"""
Google ile giriş: uygulama Credential Manager'dan aldığı ID token'ı gönderir,
sunucu Google'ın açık anahtarlarıyla doğrular (imza, aud = Web istemci kimliği, iss, süre).
Şifre saklanmaz; e-posta ve ad Google'dan gelir.
"""
import asyncio
import logging
from typing import Optional

import jwt
from fastapi import HTTPException

from . import config

log = logging.getLogger("evrota.google")
_jwks = jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs", cache_keys=True, lifespan=6 * 3600)
_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


def _verify_sync(id_token: str) -> dict:
    key = _jwks.get_signing_key_from_jwt(id_token)
    claims = jwt.decode(id_token, key.key, algorithms=["RS256"], audience=config.GOOGLE_WEB_CLIENT_ID,
                        options={"require": ["exp", "iat", "sub", "aud", "iss"]})
    if claims.get("iss") not in _ISSUERS:
        raise jwt.InvalidIssuerError("iss")
    return claims


async def verify(id_token: str) -> dict:
    if not config.GOOGLE_WEB_CLIENT_ID:
        raise HTTPException(503, "Google girişi yapılandırılmadı")
    try:
        claims = await asyncio.to_thread(_verify_sync, id_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Google oturumunun süresi dolmuş, tekrar dene")
    except Exception as e:
        log.warning("Google token doğrulanamadı: %s", e)
        raise HTTPException(401, "Google girişi doğrulanamadı")
    if not claims.get("email_verified", False):
        raise HTTPException(401, "Google hesabının e-postası doğrulanmamış")
    return {"sub": claims["sub"], "email": claims.get("email", ""), "name": claims.get("name", ""),
            "picture": claims.get("picture", "")}
