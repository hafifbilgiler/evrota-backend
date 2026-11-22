"""
EVrota backend
  GET  /health                       (açık)
  POST /auth/device                  (açık) cihaz token'ı alır
  --- aşağıdakiler Authorization: Bearer <token> ister ---
  GET  /vehicles                     araç kataloğu (uygulamadaki vehicles.json ile aynı biçim)
  GET  /prices                       elektrik / akaryakıt / şarj fiyatları
  GET  /stations?lat=&lng=&radius=&operator=  yakındaki şarj istasyonları + anlık doluluk (Google Places)
  GET  /operators                    filtrede gösterilecek operatör listesi
  POST /stations/{id}/reports        kullanıcı bildirimi: çalışıyor / kuyruk / arızalı / kapalı
  GET  /stations/{id}/reports        son 6 saatin bildirim özeti
  GET  /news?limit=&category=&before= EV haberleri (kendi özetimiz + kaynak linki)
  GET  /vehicles/{id}/insight        AI araç değerlendirmesi (katalog verisine dayalı)
  GET  /stations/{id}/pattern        doluluk deseni: "genelde ne zaman boş" (kendi topladığımız geçmiş)
  POST /auth/google                 Google ile giriş (ID token doğrulanır, hesaba bağlı token + yenileme anahtarı)
  POST /auth/logout                 çıkış
  GET  /me · DELETE /me/account     hesap bilgisi · hesabı ve tüm veriyi silme
  GET/PUT /me/data/{favorites|settings|routes|progress|trips}  favoriler, ayarlar, rotalar, rozetler, yolculuk özetleri
  GET/PUT /me/profile               kullanıcı profili (araç, ev elektrik fiyatı, alışkanlıklar) — PostgreSQL
  GET/PUT /me/sessions              şarj kayıtları senkronu
  GET  /me/analysis                 AI profil analizi (tasarruf, batarya, saat önerileri)
  POST /stations/{id}/rating · GET /stations/{id}/ratings   istasyon puanı, etiketler, yorumlar
  POST /route/plan                  şarj duraklı rota planı (araca, mevsime, rezerve göre)
  POST /agent/chat                   AI EV asistanı (Gemini + EVrota verisiyle fonksiyon çağrısı)
  PUT/DELETE /me/push               bildirim anahtarı (FCM) ve bildirim tercihleri
  POST/DELETE /stations/{id}/watch  "boşalınca haber ver" (2 saat geçerli, tek seferlik)
  GET/POST /me/premium              Premium durumu · Google Play satın almasını doğrula
  GET/PUT /me/alerts                Premium: favori istasyon ve fiyat alarmları
  GET /campaign · GET /me/campaign · PUT /me/campaign/address   lansman kupa kampanyası
  GET /shop                         mağaza (ConfigMap; kapalıyken "Yakında", açılınca Shopier ürün linkleri)
"""
import asyncio
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

from . import agent, auth, cache, campaign, config, db, google_auth, insights, news, occupancy, places, alerts, premium, profile_ai, push, route_planner, watch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # istek URL'lerini (anahtar vb.) loglama
log = logging.getLogger("evrota")

VERSION = "0.19.0"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if len(config.JWT_SECRET) < 32:
        raise RuntimeError("EVROTA_JWT_SECRET tanımlı değil ya da 32 karakterden kısa; servis başlatılmadı")
    await cache.connect()
    await db.connect()
    watcher = asyncio.create_task(watch.loop())   # "boşalınca haber ver" kontrolü
    yield
    watcher.cancel()
    await db.close()
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
    refreshToken: Optional[str] = Field(default=None, max_length=200)   # girişli kullanıcı


@app.post("/auth/device")
async def auth_device(body: DeviceIn, request: Request):
    return await auth.register_device(body.deviceId, request, body.integrityToken, body.refreshToken)


class GoogleIn(BaseModel):
    idToken: str = Field(max_length=8192)


@app.post("/auth/google")
async def auth_google(body: GoogleIn, device: str = Depends(auth.require_device)):
    """
    Google ile giriş. Uygulama mevcut cihaz token'ıyla çağırır; Google ID token'ı doğrulanır,
    hesap açılır/bulunur, cihazdaki veri hesaba taşınır, hesaba bağlı token + yenileme anahtarı döner.
    """
    if not await db.ensure():
        raise HTTPException(503, "Giriş şu an yapılamıyor")
    g = await google_auth.verify(body.idToken)
    user = await db.upsert_user(g)
    moved = await db.migrate_device_to_user(device, user["id"])
    refresh = await db.new_refresh(user["id"], device)
    return {**auth.issue_token(device, user["id"]), "refreshToken": refresh, "user": user, "moved": moved}


class LogoutIn(BaseModel):
    refreshToken: Optional[str] = Field(default=None, max_length=200)


@app.post("/auth/logout")
async def auth_logout(body: LogoutIn, device: str = Depends(auth.require_device)):
    """Çıkış: yenileme anahtarı iptal edilir, cihaz token'ı döner (veri hesapta kalır)."""
    if body.refreshToken and await db.ensure():
        await db.revoke_refresh(body.refreshToken)
    return auth.issue_token(device)


@app.get("/me")
async def me(uid: str = Depends(auth.require_user)):
    if not await db.ensure():
        raise HTTPException(503, "Şu an kullanılamıyor")
    user = await db.get_user(uid)
    if not user:
        raise HTTPException(401, "Hesap bulunamadı")
    return {"user": user}


@app.delete("/me/account")
async def delete_account(uid: str = Depends(auth.require_user)):
    """Hesabı ve tüm verisini kalıcı olarak siler."""
    if not await db.ensure():
        raise HTTPException(503, "Şu an kullanılamıyor")
    await db.delete_user(uid)
    await campaign.forget(f"u:{uid}")
    return {"ok": True}


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


# ── Mağaza ─────────────────────────────────────────────────────
# Ürünler ve açık/kapalı durumu k8s/22-configmap-shop.yaml'dan gelir. Ödeme Shopier'de alınır
# (fiziksel ürün, Play Billing gerekmez); uygulama sadece ürün linkini tarayıcıda açar.
_SHOP_URL = re.compile(r"^https://[A-Za-z0-9.-]+(/[^\s]*)?$")


def _shop_url(u) -> Optional[str]:
    return u if isinstance(u, str) and _SHOP_URL.match(u) and len(u) < 500 else None


@app.get("/shop")
async def shop(_: str = Depends(auth.require_device)):
    d = _load_json(config.SHOP_FILE) or {}
    items = []
    for p in d.get("products") or []:
        if not isinstance(p, dict) or not p.get("title"):
            continue
        items.append({"id": str(p.get("id") or p["title"])[:60], "title": str(p["title"])[:80],
                      "description": str(p.get("description") or "")[:300], "price": str(p.get("price") or "")[:30],
                      "image": _shop_url(p.get("image")), "url": _shop_url(p.get("url")),
                      "soldOut": bool(p.get("soldOut"))})
    is_open = bool(d.get("open")) and any(i["url"] for i in items)
    return {"open": is_open,
            "title": str(d.get("title") or "EVrota mağaza")[:60],
            "note": str(d.get("note") or "EVrota kupaları ve aksesuarları yakında burada.")[:300],
            "storeUrl": _shop_url(d.get("storeUrl")) if is_open else None,
            "products": items if is_open else items[:6]}   # kapalıyken ürünler "Yakında" önizlemesi olarak döner


# ── Şarj istasyonları ──────────────────────────────────────────
def _with_distance(payload: dict, lat: float, lng: float) -> dict:
    for s in payload["stations"]:
        s["distance"] = places.distance_m(lat, lng, s["lat"], s["lng"])
    payload["stations"].sort(key=lambda x: x["distance"])
    return payload


def _counts(stations: list) -> dict:
    out: dict = {}
    for s in stations:
        op = s.get("operator") or "Diğer"
        out[op] = out.get(op, 0) + 1
    return out


async def _attach_counts(payload: dict, lat: float, lng: float, radius: int, operator: Optional[str]) -> None:
    """
    Firma çiplerindeki sayılar: o yarıçaptaki TÜM istasyonlara göre.
    Firma seçiliyse, aynı yarıçapın "tüm firmalar" önbelleği varsa sayılar oradan gelir.
    """
    payload.setdefault("capped", False)
    if not operator:
        payload["operatorCounts"] = _counts(payload["stations"])
        payload["countsCapped"] = payload["capped"]
        return
    allc = await cache.get_json(f"st:{round(lat, 2)}:{round(lng, 2)}:{radius}:*")
    if allc:
        payload["operatorCounts"] = _counts(allc["stations"])
        payload["operatorCounts"][operator] = len(payload["stations"])
        payload["countsCapped"] = allc.get("capped", False)
    else:
        payload["operatorCounts"] = {operator: len(payload["stations"])}
        payload["countsCapped"] = True


async def _attach_reports(stations: list) -> None:
    """Her istasyona son kullanıcı bildirimlerinin özetini ve kullanıcı puanını ekler."""
    for s in stations:
        s["reports"] = await _report_summary(s["id"])
    if stations and db.ready():
        try:
            avgs = await db.rating_avgs([s["id"] for s in stations])
            for s in stations:
                s["userRating"] = avgs.get(s["id"])
        except Exception as e:
            log.warning("Puanlar eklenemedi: %s", e)


@app.get("/operators")
async def operators(_: str = Depends(auth.require_device)):
    return {"operators": places.OPERATOR_NAMES}


@app.get("/stations")
async def stations(lat: float, lng: float, radius: int = 5000, operator: Optional[str] = None,
                   _: str = Depends(auth.require_device)):
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(400, "Geçersiz konum")
    if operator and operator not in places.OPERATOR_NAMES:
        raise HTTPException(400, "Geçersiz operatör")
    return await _stations_payload(lat, lng, radius, operator)


async def _stations_payload(lat: float, lng: float, radius: int, operator: Optional[str]) -> dict:
    """/stations ve AI asistanı aynı önbelleği ve günlük Places sınırını paylaşır."""
    radius = max(1000, min(radius, 30000))
    cell = f"{round(lat, 2)}:{round(lng, 2)}:{radius}:{operator or '*'}"
    key, stale_key = f"st:{cell}", f"st_stale:{cell}"

    cached = await cache.get_json(key)
    if cached:
        payload = _with_distance(cached, lat, lng)
        await _attach_counts(payload, lat, lng, radius, operator)
        await _attach_reports(payload["stations"])
        return payload

    used = await cache.incr(f"cap:{datetime.now(timezone.utc).strftime('%Y%m%d')}", 90000)
    over_cap = used > config.DAILY_PLACES_CAP
    if over_cap:
        log.warning("Günlük Places sınırı aşıldı (%d)", used)

    found = None if over_cap else await places.search_stations(lat, lng, radius, operator)
    if found is not None:
        # Sayfalı aramada fazladan yapılan Google çağrıları da günlük sınıra sayılır
        for _ in range(max(0, found["calls"] - 1)):
            await cache.incr(f"cap:{datetime.now(timezone.utc).strftime('%Y%m%d')}", 90000)
        result = found["stations"]
        await occupancy.record(result)
        payload = {"stations": result, "capped": found["capped"], "fetched": int(time.time()), "stale": False}
        await cache.set_json(key, payload, config.STATIONS_CACHE_TTL if result else 600)
        if result:
            await cache.set_json(stale_key, payload, config.STATIONS_STALE_TTL)
        await _attach_counts(payload, lat, lng, radius, operator)
        await _attach_reports(payload["stations"])
        return payload

    stale = await cache.get_json(stale_key)
    if stale:
        stale["stale"] = True
        payload = _with_distance(stale, lat, lng)
        await _attach_counts(payload, lat, lng, radius, operator)
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


# ── Haberler ───────────────────────────────────────────────────
@app.get("/news")
async def get_news(limit: int = 30, category: Optional[str] = None, before: Optional[int] = None,
                   _: str = Depends(auth.require_device)):
    limit = max(1, min(limit, 50))
    if category and category not in news.CATEGORIES:
        raise HTTPException(400, "Geçersiz kategori")
    items = await news.latest(limit, category, before)
    # Haberdeki araç katalogda varsa teknik özetini ekle
    cat = {v["id"]: v for v in news._catalog()}
    for it in items:
        v = cat.get(it.get("vehicleId") or "")
        it["vehicle"] = None if v is None else {
            "id": v["id"], "name": f'{v["brand"]} {v["model"]}', "batteryKwh": v["batteryKwh"],
            "rangeWltp": v["rangeWltp"], "dcMaxKw": v["dcMaxKw"], "dc10to80Min": v["dc10to80Min"]}
    return {"items": items, "count": len(items)}


@app.get("/stations/{station_id}/pattern")
async def station_pattern(station_id: str, _: str = Depends(auth.require_device)):
    """Doluluk deseni: haftanın günü x saat boşluk oranı, şimdiki tahmin, bugünün en boş saatleri."""
    if not _ID_RE.match(station_id):
        raise HTTPException(400, "Geçersiz istasyon")
    res = await occupancy.pattern(station_id)
    if res is None:
        raise HTTPException(503, "Şu an kullanılamıyor")
    return res


# ── Profil (PostgreSQL) ────────────────────────────────────────
class ProfileIn(BaseModel):
    vehicleId: Optional[str] = Field(default=None, max_length=80)
    homePriceKwh: Optional[float] = Field(default=None, ge=0, le=100)
    hasHomeCharger: Optional[bool] = None
    homeChargerKw: Optional[float] = Field(default=None, ge=0, le=50)
    dailyKm: Optional[int] = Field(default=None, ge=0, le=2000)
    batteryHealth: Optional[int] = Field(default=None, ge=40, le=100)
    preferredOperators: list[str] = Field(default_factory=list, max_length=20)
    chargeLimit: Optional[int] = Field(default=None, ge=50, le=100)
    climate: Optional[str] = Field(default=None, max_length=30)
    usage: Optional[str] = Field(default=None, max_length=30)
    city: Optional[str] = Field(default=None, max_length=40)
    tariff: Optional[str] = Field(default=None, pattern="^(tek|uc_zamanli)$")      # ev elektrik tarifesi
    nightPriceKwh: Optional[float] = Field(default=None, ge=0, le=100)             # üç zamanlı: gece (22-06)
    dayPriceKwh: Optional[float] = Field(default=None, ge=0, le=100)               # gündüz (06-17)
    peakPriceKwh: Optional[float] = Field(default=None, ge=0, le=100)              # puant (17-22)
    reminderHour: Optional[int] = Field(default=None, ge=0, le=23)


class SessionIn(BaseModel):
    id: str = Field(max_length=64)
    vehicleId: Optional[str] = Field(default=None, max_length=80)
    stationId: Optional[str] = Field(default=None, max_length=300)
    stationName: str = Field(default="", max_length=120)
    kwh: float = Field(ge=0, le=500)
    cost: float = Field(ge=0, le=100000)
    socFrom: Optional[int] = Field(default=None, ge=0, le=100)
    socTo: Optional[int] = Field(default=None, ge=0, le=100)
    isDc: bool = False
    chargedAt: int = Field(gt=0)


async def _need_db():
    if not await db.ensure():
        raise HTTPException(503, "Profil şu an kullanılamıyor")


@app.get("/me/profile")
async def get_my_profile(device: str = Depends(auth.require_owner)):
    await _need_db()
    return {"profile": await db.get_profile(device)}


@app.put("/me/profile")
async def put_my_profile(body: ProfileIn, device: str = Depends(auth.require_owner)):
    await _need_db()
    await db.put_profile(device, body.model_dump())
    return {"ok": True}


@app.put("/me/sessions")
async def put_my_sessions(body: list[SessionIn], allowEmpty: int = 0, device: str = Depends(auth.require_owner)):
    """Telefondaki şarj kayıtlarının tamamı (en fazla 1000) — sunucudaki liste bununla değişir.
    Koruma: boş liste, sunucuda kayıt varken sadece allowEmpty=1 ile kabul edilir (kullanıcı son kaydı sildiyse).
    Yeni telefonda henüz veri çekilmeden gelen boş liste hesaptaki kayıtları silmesin."""
    await _need_db()
    if len(body) > 1000:
        raise HTTPException(400, "En fazla 1000 kayıt")
    if not body and not allowEmpty:
        existing = await db.get_sessions(device)
        if existing:
            log.info("Boş şarj listesi yok sayıldı (%s, sunucuda %d kayıt)", device[:12], len(existing))
            return {"ok": True, "count": len(existing), "ignored": True}
    n = await db.replace_sessions(device, [s.model_dump() for s in body])
    return {"ok": True, "count": n}


@app.get("/me/sessions")
async def get_my_sessions(device: str = Depends(auth.require_owner)):
    await _need_db()
    return {"sessions": await db.get_sessions(device)}


_DATA_KEYS = {"favorites", "settings", "routes", "progress", "trips"}   # progress: rozet ve sayaçlar; trips: yolculuk özetleri (konum yok)


def _merge_progress(old, new: dict) -> dict:
    """Rozetler: her seviye için en eski kazanma tarihi; sayaçlar: büyük olan; ilk kullanım: küçük olan."""
    if not isinstance(old, dict):
        return new
    ob, nb = old.get("badges") or {}, new.get("badges") or {}
    badges = {}
    for bid in set(ob) | set(nb):
        a = list(ob.get(bid) or []) + [0, 0, 0]
        b = list(nb.get(bid) or []) + [0, 0, 0]
        badges[bid] = [min([x for x in (a[k], b[k]) if isinstance(x, (int, float)) and x > 0], default=0) for k in range(3)]
    oc, nc = old.get("counters") or {}, new.get("counters") or {}
    counters = {k: max(int(oc.get(k) or 0), int(nc.get(k) or 0)) for k in ("rated", "reports")}
    fs = [int(x) for x in (oc.get("firstSeen"), nc.get("firstSeen")) if isinstance(x, (int, float)) and x > 0]
    counters["firstSeen"] = min(fs) if fs else 0
    return {**old, **new, "badges": badges, "counters": counters}


@app.get("/me/data/{key}")
async def get_my_data(key: str, owner: str = Depends(auth.require_owner)):
    """Favori istasyonlar, ayarlar gibi küçük kullanıcı verileri."""
    if key not in _DATA_KEYS:
        raise HTTPException(404, "Bilinmeyen veri")
    await _need_db()
    return {"data": await db.get_data(owner, key)}


@app.put("/me/data/{key}")
async def put_my_data(key: str, request: Request, owner: str = Depends(auth.require_owner)):
    if key not in _DATA_KEYS:
        raise HTTPException(404, "Bilinmeyen veri")
    await _need_db()
    raw = await request.body()
    if len(raw) > 64_000:
        raise HTTPException(413, "Veri çok büyük")
    try:
        data = json.loads(raw or b"null")
    except ValueError:
        raise HTTPException(400, "Geçersiz JSON")
    if key == "progress" and isinstance(data, dict):
        data = _merge_progress(await db.get_data(owner, key), data)   # rozet seviyesi hiçbir zaman geri alınmaz
    await db.put_data(owner, key, data)
    return {"ok": True}


@app.get("/me/analysis")
async def my_analysis(device: str = Depends(auth.require_owner)):
    """AI profil analizi: sunucu istatistik hesaplar, Gemini yorumlar (12 saat önbellek, günlük sınır)."""
    await _need_db()
    profile = await db.get_profile(device) or {}
    sessions = await db.get_sessions(device)
    lim = config.PROFILE_ANALYSIS_DAILY_PREMIUM if await premium.is_premium(device) else None
    return await profile_ai.analyze(device, profile, sessions, lim)


# ── İstasyon puanlama ──────────────────────────────────────────
RATING_TAGS = {"tuvalet", "kafe", "market", "yemek", "kapali_alan", "gece_guvenli", "engelli",
               "acik_7_24", "sik_ariza", "sik_kuyruk", "temiz", "aydinlik"}


class RatingIn(BaseModel):
    stars: int = Field(ge=1, le=5)
    tags: list[str] = Field(default_factory=list, max_length=12)
    comment: Optional[str] = Field(default=None, max_length=280)


@app.post("/stations/{station_id}/rating")
async def rate_station(station_id: str, body: RatingIn, owner: str = Depends(auth.require_owner)):
    """İstasyonu puanla (kişi başına 1 puan, tekrar gönderilirse güncellenir)."""
    if not _ID_RE.match(station_id):
        raise HTTPException(400, "Geçersiz istasyon")
    await _need_db()
    tags = sorted({t for t in body.tags if t in RATING_TAGS})
    comment = (body.comment or "").strip() or None
    await db.put_rating(station_id, owner, body.stars, tags, comment)
    return {"ok": True, "summary": await db.rating_summary(station_id, owner)}


@app.get("/stations/{station_id}/ratings")
async def station_ratings(station_id: str, owner: str = Depends(auth.require_owner)):
    if not _ID_RE.match(station_id):
        raise HTTPException(400, "Geçersiz istasyon")
    await _need_db()
    return await db.rating_summary(station_id, owner)


# ── Rota planlayıcı ────────────────────────────────────────────
class RouteIn(BaseModel):
    originLat: float = Field(ge=-90, le=90)
    originLng: float = Field(ge=-180, le=180)
    destination: Optional[str] = Field(default=None, max_length=160)
    destLat: Optional[float] = Field(default=None, ge=-90, le=90)
    destLng: Optional[float] = Field(default=None, ge=-180, le=180)
    destPlaceId: Optional[str] = Field(default=None, max_length=300)
    routeIndex: int = Field(default=0, ge=0, le=2)
    vehicleId: Optional[str] = Field(default=None, max_length=80)
    startSoc: int = Field(default=80, ge=5, le=100)
    reserveSoc: int = Field(default=10, ge=5, le=30)
    minKw: int = Field(default=50, ge=22, le=350)


@app.post("/route/plan")
async def route_plan(body: RouteIn, request: Request, device: str = Depends(auth.require_device)):
    """Araca göre şarj duraklı rota (Google Routes + rota üzerinde istasyon araması)."""
    origin = {"location": {"latLng": {"latitude": round(body.originLat, 3), "longitude": round(body.originLng, 3)}}}
    if body.destPlaceId:
        dest = {"placeId": body.destPlaceId}
    elif body.destLat is not None and body.destLng is not None:
        dest = {"location": {"latLng": {"latitude": round(body.destLat, 3), "longitude": round(body.destLng, 3)}}}
    elif body.destination and body.destination.strip():
        dest = {"address": body.destination.strip() + ", Türkiye"}
    else:
        raise HTTPException(400, "Varış yeri gerekli")
    lim = config.ROUTE_DAILY_LIMIT_PREMIUM if await premium.is_premium(auth.owner_of(request)) else None
    res = await route_planner.plan(device, origin, dest, body.vehicleId, body.startSoc, body.reserveSoc, body.minKw,
                                   body.routeIndex, lim)
    if "error" in res:
        raise HTTPException(422, res["error"])
    return res


@app.get("/weather")
async def get_weather(lat: float, lng: float, _: str = Depends(auth.require_device)):
    """Anlık sıcaklık/rüzgâr ve bu havada otoyol menzil oranı (menzil hesaplayıcı için)."""
    w = await route_planner.weather(lat, lng)
    if not w:
        raise HTTPException(503, "Hava durumu şu an alınamıyor")
    return {**w, "highwayFactor": route_planner.range_factor(w["tempC"], w["windKmh"])}


@app.get("/places/autocomplete")
async def places_autocomplete(q: str, lat: Optional[float] = None, lng: Optional[float] = None,
                              device: str = Depends(auth.require_device)):
    """Rota varış yeri önerileri (3+ harf)."""
    day = time.strftime("%Y%m%d", time.gmtime())
    if await cache.incr(f"ac:d:{device}:{day}", 90000) > 300:
        return {"suggestions": []}
    return {"suggestions": await route_planner.autocomplete(q, lat, lng)}


# ── AI asistan ─────────────────────────────────────────────────
class ChatMsg(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    text: str = Field(max_length=4000)


class ChatIn(BaseModel):
    messages: list[ChatMsg] = Field(max_length=30)
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)
    vehicleId: Optional[str] = Field(default=None, max_length=80)


@app.post("/agent/chat")
async def agent_chat(body: ChatIn, request: Request, device: str = Depends(auth.require_device)):
    ctx = {"lat": body.lat if body.lng is not None else None, "lng": body.lng, "vehicleId": body.vehicleId}
    lim = config.AGENT_DAILY_LIMIT_PREMIUM if await premium.is_premium(auth.owner_of(request)) else None
    return await agent.chat(device, [m.model_dump() for m in body.messages], ctx, _stations_payload, lim)


@app.get("/vehicles/{vehicle_id}/insight")
async def vehicle_insight(vehicle_id: str, _: str = Depends(auth.require_device)):
    if not _ID_RE.match(vehicle_id):
        raise HTTPException(400, "Geçersiz araç")
    res = await insights.get_insight(vehicle_id)
    if res is None:
        raise HTTPException(404, "Araç bulunamadı")
    return res


# ── Bildirimler ────────────────────────────────────────────────
class PushIn(BaseModel):
    token: str = Field(min_length=20, max_length=4096)
    prefs: dict = Field(default_factory=dict)


@app.put("/me/push")
async def put_push(body: PushIn, owner: str = Depends(auth.require_owner)):
    await _need_db()
    prefs = {k: bool(v) for k, v in body.prefs.items() if k in ("stationFree", "favorites", "news")}
    await db.put_push_token(owner, body.token, prefs)
    return {"ok": True, "enabled": push.enabled()}


class PushDel(BaseModel):
    token: str = Field(max_length=4096)


@app.delete("/me/push")
async def delete_push(body: PushDel, _: str = Depends(auth.require_owner)):
    await _need_db()
    await db.delete_push_tokens([body.token])
    return {"ok": True}


class WatchIn(BaseModel):
    name: str = Field(default="", max_length=200)
    lat: float = 0.0
    lng: float = 0.0


@app.post("/stations/{station_id}/watch")
async def watch_station(station_id: str, body: WatchIn, owner: str = Depends(auth.require_owner)):
    if not _ID_RE.match(station_id):
        raise HTTPException(400, "Geçersiz istasyon")
    await _need_db()
    if not push.enabled():
        raise HTTPException(503, "Bildirimler şu an kullanılamıyor")
    if not await db.has_push_token(owner):
        raise HTTPException(409, "Bildirim kaydı bulunamadı. Uygulamayı yeniden açıp tekrar dene.")
    if await watch.active_count(owner) >= config.WATCH_MAX_PER_OWNER:
        raise HTTPException(429, f"Aynı anda en fazla {config.WATCH_MAX_PER_OWNER} istasyonu takip edebilirsin")
    try:
        until = await watch.add(owner, station_id, body.name, body.lat, body.lng)
    except RuntimeError:
        raise HTTPException(503, "Takip şu an başlatılamıyor")
    return {"ok": True, "expiresAt": until}


@app.delete("/stations/{station_id}/watch")
async def unwatch_station(station_id: str, owner: str = Depends(auth.require_owner)):
    await watch.remove(owner, station_id)
    return {"ok": True}


# ── Premium (Google Play aboneliği) ────────────────────────────
class PremiumIn(BaseModel):
    purchaseToken: str = Field(min_length=20, max_length=4096)
    productId: str = Field(default="", max_length=100)


@app.post("/me/premium")
async def register_premium(body: PremiumIn, owner: str = Depends(auth.require_owner)):
    await _need_db()
    if body.productId and body.productId != config.PREMIUM_PRODUCT:
        raise HTTPException(400, "Geçersiz ürün")
    v = await premium.register(owner, body.purchaseToken)
    if v is None:
        raise HTTPException(422, "Satın alma doğrulanamadı. Birazdan tekrar dene.")
    slot = None
    try:
        slot = await campaign.try_reserve(owner, v)
    except Exception as e:
        log.warning("Kampanya sırası verilemedi: %s", e)
    return {"active": v["active"], "expiresAt": v["expires"], "campaignSlot": slot}


@app.get("/me/premium")
async def get_premium(owner: str = Depends(auth.require_owner)):
    s = await premium.status(owner)
    return {"active": s.get("active", False), "expiresAt": s.get("expires", 0)}


# ── Premium alarmları ─────────────────────────────────────────
class AlertStation(BaseModel):
    id: str = Field(max_length=300)
    name: str = Field(default="", max_length=200)
    lat: float = 0.0
    lng: float = 0.0


class AlertsIn(BaseModel):
    stations: list[AlertStation] = Field(default_factory=list, max_length=alerts.MAX_STATIONS)
    price: bool = False


@app.get("/me/alerts")
async def get_alerts(owner: str = Depends(auth.require_owner)):
    await _need_db()
    return {"alerts": await db.get_data(owner, "alerts") or {"stations": [], "price": False},
            "premium": await premium.is_premium(owner), "max": alerts.MAX_STATIONS}


@app.put("/me/alerts")
async def put_alerts(body: AlertsIn, owner: str = Depends(auth.require_owner)):
    await _need_db()
    if (body.stations or body.price) and not await premium.is_premium(owner):
        raise HTTPException(403, "Alarmlar EVrota Premium ile kullanılabilir")
    for s in body.stations:
        if not _ID_RE.match(s.id):
            raise HTTPException(400, "Geçersiz istasyon")
    data = {"stations": [s.model_dump() for s in body.stations], "price": body.price}
    await db.put_data(owner, "alerts", data)
    return {"ok": True, "alerts": data}


# ── Lansman kampanyası (ilk 10 premium üyeye kupa) ─────────────
@app.get("/campaign")
async def campaign_info(_: str = Depends(auth.require_device)):
    if not config.CAMPAIGN_LIMIT or not await db.ensure():
        return {"active": False, "limit": 0, "remaining": 0}
    left = await campaign.remaining()
    return {"active": left > 0, "limit": config.CAMPAIGN_LIMIT, "remaining": left}


@app.get("/me/campaign")
async def my_campaign(owner: str = Depends(auth.require_owner)):
    await _need_db()
    try:
        await campaign.check(owner)
    except Exception as ex:
        log.warning("Kampanya kontrolü: %s", ex)
    e = await campaign.entry(owner, await premium.is_premium(owner))
    left = await campaign.remaining() if config.CAMPAIGN_LIMIT else 0
    return {"entry": e, "signedIn": owner.startswith("u:"), "remaining": left}


class AddressIn(BaseModel):
    fullName: str = Field(min_length=3, max_length=100)
    phone: str = Field(min_length=10, max_length=20, pattern=r"^[0-9 +()-]+$")
    address: str = Field(min_length=10, max_length=300)
    district: str = Field(min_length=2, max_length=60)
    city: str = Field(min_length=2, max_length=40)
    postalCode: str = Field(default="", max_length=10)
    brand: str = Field(default="", max_length=40)


@app.put("/me/campaign/address")
async def campaign_address(body: AddressIn, uid: str = Depends(auth.require_user)):
    await _need_db()
    addr = body.model_dump(exclude={"brand"})
    if not await campaign.set_address(f"u:{uid}", addr, body.brand.strip()):
        raise HTTPException(404, "Kampanya kaydın bulunamadı ya da kupan zaten kargoya verildi")
    log.info("Kampanya: u:%s adres girdi", uid)
    return {"ok": True}
