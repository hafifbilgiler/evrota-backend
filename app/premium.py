"""
EVrota Premium: Google Play aboneliğinin sunucuda doğrulanması.
Uygulama satın alma anahtarını (purchaseToken) gönderir; sunucu Play Developer API'ye sorar
(subscriptionsv2) ve sonucu PostgreSQL'e yazar. Sınırlar (asistan, rota, analiz) buna göre açılır.
Servis hesabı: Firebase ile aynı (FIREBASE_SA_FILE); Play Console'da EVrota için
"Finansal verileri görüntüleme" + "Siparişleri ve abonelikleri yönetme" izni verilmiş olmalı.
"""
import logging
import time
from datetime import datetime
from typing import Optional

import httpx

from . import cache, config, db, push

log = logging.getLogger("evrota.premium")
_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
# İptal edilmiş (otomatik yenilemesi kapalı) abonelik de süresi bitene kadar geçerlidir
_VALID = {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD", "SUBSCRIPTION_STATE_CANCELED"}


def _ts(s: Optional[str]) -> int:
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


async def verify(token: str) -> Optional[dict]:
    """Play'e sorar. Dönüş: {state, expires, product, test, active} ya da None (geçersiz / ulaşılamadı)."""
    url = (f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
           f"{config.PLAY_PACKAGE}/purchases/subscriptionsv2/tokens/{token}")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            access = await push.google_token(client, _SCOPE)
            if not access:
                return None
            r = await client.get(url, headers={"Authorization": f"Bearer {access}"})
        if r.status_code != 200:
            log.warning("Play abonelik doğrulama HTTP %s", r.status_code)
            return None
        d = r.json()
    except Exception as e:
        log.warning("Play abonelik doğrulama hatası: %s", e)
        return None
    items = [i for i in d.get("lineItems", []) if i.get("productId") == config.PREMIUM_PRODUCT]
    if not items:
        return None
    expires = max(_ts(i.get("expiryTime")) for i in items)
    state = d.get("subscriptionState", "")
    active = state in _VALID and expires > time.time()
    return {"state": state, "expires": expires, "product": config.PREMIUM_PRODUCT,
            "test": "testPurchase" in d, "active": active, "paid": active and _paid(items)}


def _paid(items: list) -> bool:
    """Ücretsiz deneme bitti, en az bir ödeme alındı mı?
    offerPhase varsa ona bakılır (freeTrial = deneme). Yoksa: yenileme siparişlerinin numarası
    "GPA.xxxx..0" gibi ".." içerir; ilk sipariş deneme siparişidir."""
    for i in items:
        ph = i.get("offerPhase")
        if isinstance(ph, dict) and ph:
            if "freeTrial" not in ph:
                return True
            continue
        if ".." in (i.get("latestSuccessfulOrderId") or ""):
            return True
    return False


async def register(owner: str, token: str) -> Optional[dict]:
    """Uygulamadan gelen satın almayı doğrular ve sahibine bağlar."""
    v = await verify(token)
    if v is None:
        return None
    await db.put_premium(owner, token, v["product"], v["state"], v["expires"], v["test"])
    await cache.set_json(f"prem:{owner}", {"active": v["active"], "expires": v["expires"]}, 600)
    return v


async def status(owner: str) -> dict:
    """Premium durumu (10 dk önbellek). Süresi geçmişse yenilenmiş mi diye Play'e en fazla saatte bir sorar."""
    hit = await cache.get_json(f"prem:{owner}")
    if hit is not None:
        return hit
    out = {"active": False, "expires": 0}
    if await db.ensure():
        row = await db.latest_premium(owner)
        if row:
            out = {"active": row["exp"] > time.time() and row["state"] in _VALID, "expires": row["exp"]}
            if not out["active"] and time.time() - row["checked"] > 3600:
                v = await verify(row["purchase_token"])
                if v:
                    await db.put_premium(owner, row["purchase_token"], v["product"], v["state"], v["expires"], v["test"])
                    out = {"active": v["active"], "expires": v["expires"]}
    await cache.set_json(f"prem:{owner}", out, 600)
    return out


async def is_premium(owner: str) -> bool:
    try:
        return bool((await status(owner)).get("active"))
    except Exception:
        return False
