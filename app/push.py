"""
Firebase Cloud Messaging (HTTP v1) ile telefona bildirim.
Servis hesabı anahtarı k8s secret'ından dosya olarak bağlanır (FIREBASE_SA_FILE).
Dosya yoksa bildirim kapalıdır; API yine çalışır.
"""
import json
import logging
import os
import time
from typing import Optional

import httpx
import jwt

from . import config

log = logging.getLogger("evrota.push")
_sa: Optional[dict] = None
_tokens: dict = {}   # kapsam → (erişim anahtarı, bitiş)


def _account() -> Optional[dict]:
    global _sa
    if _sa is None and config.FIREBASE_SA_FILE and os.path.exists(config.FIREBASE_SA_FILE):
        try:
            with open(config.FIREBASE_SA_FILE, encoding="utf-8") as f:
                _sa = json.load(f)
            log.info("FCM açık (proje: %s)", _sa.get("project_id"))
        except Exception as e:
            log.error("Firebase servis hesabı okunamadı: %s", e)
    return _sa


def enabled() -> bool:
    return _account() is not None


async def _access_token(client: httpx.AsyncClient) -> Optional[str]:
    return await google_token(client, "https://www.googleapis.com/auth/firebase.messaging")


async def google_token(client: httpx.AsyncClient, scope: str) -> Optional[str]:
    """Servis hesabıyla imzalı JWT → Google OAuth erişim anahtarı (kapsam başına 50 dk önbellek)."""
    hit = _tokens.get(scope)
    if hit and time.time() < hit[1]:
        return hit[0]
    sa = _account()
    if not sa:
        return None
    now = int(time.time())
    assertion = jwt.encode({
        "iss": sa["client_email"], "scope": scope,
        "aud": sa.get("token_uri", "https://oauth2.googleapis.com/token"), "iat": now, "exp": now + 3600,
    }, sa["private_key"], algorithm="RS256")
    r = await client.post(sa.get("token_uri", "https://oauth2.googleapis.com/token"),
                          data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
    if r.status_code != 200:
        log.error("Google erişim anahtarı alınamadı (%s): HTTP %s", scope.rsplit("/", 1)[-1], r.status_code)
        return None
    token = r.json()["access_token"]
    _tokens[scope] = (token, time.time() + 3000)
    return token


async def send(tokens: list[str], title: str, body: str, data: dict) -> list[str]:
    """Bildirimi gönderir. Geçersiz (silinmiş uygulama vb.) anahtarları döner ki veritabanından silinsin."""
    sa = _account()
    if not sa or not tokens:
        return []
    invalid = []
    payload_data = {k: str(v) for k, v in {**data, "title": title, "body": body}.items() if v is not None}
    url = f"https://fcm.googleapis.com/v1/projects/{sa['project_id']}/messages:send"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            access = await _access_token(client)
            if not access:
                return []
            for t in tokens:
                msg = {"message": {"token": t, "data": payload_data, "android": {"priority": "HIGH", "ttl": "1800s"}}}
                r = await client.post(url, json=msg, headers={"Authorization": f"Bearer {access}"})
                if r.status_code in (404, 400) and ("UNREGISTERED" in r.text or "INVALID_ARGUMENT" in r.text):
                    invalid.append(t)
                elif r.status_code != 200:
                    log.warning("FCM HTTP %s", r.status_code)
    except Exception as e:
        log.warning("FCM gönderim hatası: %s", e)
    return invalid
