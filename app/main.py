"""
EVrota backend
  GET  /health                       (açık)
  POST /auth/device                  (açık) cihaz token'ı alır
  --- aşağıdakiler Authorization: Bearer <token> ister ---
  GET  /vehicles                     araç kataloğu (uygulamadaki vehicles.json ile aynı biçim)
  GET  /prices                       elektrik / akaryakıt / şarj fiyatları
  GET  /stations?lat=&lng=&radius=   yakındaki şarj istasyonları + anlık doluluk (Google Places)
  POST /stations/{id}/reports        kullanıcı bildirimi: çalışıyor / kuyruk / arızalı / kapalı
  GET  /stations/{id}/reports        son 6 saatin bildirim özeti
"""
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field

from . import auth, cache, config, places

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("evrota")

VERSION = "0.2.0"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if len(config.JWT_SECRET) < 32:
        raise RuntimeError("EVROTA_JWT_SECRET tanımlı değil ya da 32 karakterden kısa; servis başlatılmadı")
    await cache.connect()
    yield
    await cache.close()


# Dokümantasyon sayfaları kapalı: API yüzeyi dışarıya listelenmez
app = FastAPI(title="EVrota API", version=VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def hardening(request: Request, call_next):
    # Büyük gövdeleri reddet (ör. bildirim uçlarına çöp yükleme)
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > config.MAX_BODY_BYTES:
        return JSONResponse({"detail": "İstek çok büyük"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("Beklenmeyen hata: %s", exc)
    return JSONResponse({"detail": "Sunucu hatası"}, status_code=500)


# ── Kimlik doğrulama ───────────────────────────────────────────
class DeviceIn(BaseModel):
    deviceId: str = Field(max_length=64)
    integrityToken: Optional[str] = Field(default=None, max_length=8192)


@app.post("/auth/device")
async def auth_device(body: DeviceIn, request: Request):
    return await auth.register_device(body.deviceId, request, body.integrityToken)


def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.error("%s okunamadı: %s", path, e)
        return None


# ── Sağlık ─────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION}


@app.get("/health/details")
async def health_details(_: str = Depends(auth.require_device)):
    return {"status": "ok", "version": VERSION, "redis": cache.client() is not None,
            "places": bool(config.GOOGLE_PLACES_API_KEY)}


# ── Araç kataloğu ──────────────────────────────────────────────
@app.get("/vehicles")
async def vehicles(_: str = Depends(auth.require_device)):
    data = _load_json(config.VEHICLES_FILE)
    if not data:
        raise HTTPException(503, "Araç kataloğu şu an yüklenemedi")
    return data


# ── Fiyatlar ───────────────────────────────────────────────────
# İlk sürümde fiyatlar ConfigMap'ten gelen prices.json'dan okunur (elle güncellenir).
# Otomatik toplama (EPDK, akaryakıt, operatör tarifeleri) sonraki adımda bu dosyayı yazacak.
@app.get("/prices")
async def prices(_: str = Depends(auth.require_device)):
    data = _load_json(config.PRICES_FILE)
    if not data:
        raise HTTPException(503, "Fiyat bilgisi henüz tanımlı değil")
    return data


# ── Şarj istasyonları ──────────────────────────────────────────
def _with_distance(payload: dict, lat: float, lng: float) -> dict:
    for s in payload["stations"]:
        s["distance"] = places.distance_m(lat, lng, s["lat"], s["lng"])
    payload["stations"].sort(key=lambda x: x["distance"])
    return payload


async def _attach_reports(stations: list) -> None:
    """Her istasyona son kullanıcı bildirimlerinin özetini ekler."""
    for s in stations:
        s["reports"] = await _report_summary(s["id"])


@app.get("/stations")
async def stations(lat: float, lng: float, radius: int = 5000, _: str = Depends(auth.require_device)):
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(400, "Geçersiz konum")
    radius = max(1000, min(radius, 30000))
    cell = f"{round(lat, 2)}:{round(lng, 2)}:{radius}"
    key, stale_key = f"st:{cell}", f"st_stale:{cell}"

    cached = await cache.get_json(key)
    if cached:
        payload = _with_distance(cached, lat, lng)
        await _attach_reports(payload["stations"])
        return payload

    used = await cache.incr(f"cap:{datetime.now(timezone.utc).strftime('%Y%m%d')}", 90000)
    over_cap = used > config.DAILY_PLACES_CAP
    if over_cap:
        log.warning("Günlük Places sınırı aşıldı (%d)", used)

    result = None if over_cap else await places.search_stations(lat, lng, radius)
    if result is not None:
        payload = {"stations": result, "fetched": int(time.time()), "stale": False}
        await cache.set_json(key, payload, config.STATIONS_CACHE_TTL if result else 600)
        if result:
            await cache.set_json(stale_key, payload, config.STATIONS_STALE_TTL)
        await _attach_reports(payload["stations"])
        return payload

    stale = await cache.get_json(stale_key)
    if stale:
        stale["stale"] = True
        payload = _with_distance(stale, lat, lng)
        await _attach_reports(payload["stations"])
        return payload
    raise HTTPException(503, "İstasyon bilgisi şu an alınamıyor")


# ── Kullanıcı bildirimleri ─────────────────────────────────────
REPORT_TYPES = {"Calisiyor", "Kuyruk", "Arizali", "Kapali"}
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{3,300}$")


class ReportIn(BaseModel):
    type: str = Field(max_length=20)
    note: str = Field(default="", max_length=200)
    id: Optional[str] = Field(default=None, max_length=64)   # uygulamanın UUID'si (tekrar gönderimde çift kayıt olmasın)


async def _report_summary(station_id: str) -> dict:
    r = cache.client()
    empty = {"Calisiyor": 0, "Kuyruk": 0, "Arizali": 0, "Kapali": 0, "last": None, "lastType": None}
    if r is None:
        return empty
    try:
        cutoff = time.time() - config.REPORT_TTL
        await r.zremrangebyscore(f"rep:{station_id}", 0, cutoff)
        items = await r.zrange(f"rep:{station_id}", 0, -1, withscores=True)
    except Exception:
        return empty
    out = dict(empty)
    for raw, score in items:
        try:
            t = json.loads(raw)["type"]
        except Exception:
            continue
        if t in REPORT_TYPES:
            out[t] += 1
            if out["last"] is None or score > out["last"]:
                out["last"], out["lastType"] = int(score), t
    return out


@app.post("/stations/{station_id}/reports")
async def add_report(station_id: str, body: ReportIn, device: str = Depends(auth.require_device)):
    if not _ID_RE.match(station_id):
        raise HTTPException(400, "Geçersiz istasyon")
    if body.type not in REPORT_TYPES:
        raise HTTPException(400, "Geçersiz bildirim türü")
    n = await cache.incr(f"repl:{device}:{int(time.time() // 3600)}", 3700)
    if n > config.REPORTS_PER_DEVICE_HOUR:
        raise HTTPException(429, "Çok fazla bildirim, biraz sonra tekrar deneyin")
    r = cache.client()
    if r is None:
        raise HTTPException(503, "Bildirim şu an kaydedilemiyor")
    rid = body.id or str(uuid.uuid4())
    # Aynı cihaz aynı istasyona en fazla 10 dakikada bir bildirim verebilir
    if not await r.set(f"repdev:{device}:{station_id}", "1", ex=600, nx=True):
        return {"ok": True, "id": rid, "throttled": True, "summary": await _report_summary(station_id)}
    if not await r.set(f"repid:{device}:{rid}", "1", ex=config.REPORT_TTL, nx=True):
        return {"ok": True, "id": rid, "duplicate": True}
    now = time.time()
    await r.zadd(f"rep:{station_id}", {json.dumps({"id": rid, "dev": device[:8], "type": body.type,
                                                   "note": body.note[:200]}): now})
    await r.expire(f"rep:{station_id}", config.REPORT_TTL)
    return {"ok": True, "id": rid, "summary": await _report_summary(station_id)}


@app.get("/stations/{station_id}/reports")
async def get_reports(station_id: str, _: str = Depends(auth.require_device)):
    if not _ID_RE.match(station_id):
        raise HTTPException(400, "Geçersiz istasyon")
    return await _report_summary(station_id)
