"""
EV haber tarayıcısı

- Kaynaklar: NEWS_FEEDS_FILE (ConfigMap) içindeki RSS akışları.
  Yayıncıların kendi RSS'leri ana kaynak; Google News RSS sadece keşif için.
- Tekrar eden haberler (aynı link / çok benzer başlık) birleştirilir.
- GEMINI_API_KEY varsa: EV ile ilgisi olmayanlar elenir, haber kendi cümlelerimizle
  kısa özetlenir, kategori/marka/model/fiyat çıkarılır. Yoksa anahtar kelime filtresi.
- Sonuç Redis'e yazılır (14 gün). Yeni model / fiyat haberleri katalog taslağı olarak
  ayrıca saklanır (onay için).
Haber metni kopyalanmaz: başlık + kendi özetimiz + kaynak adı + orijinal link.
"""
import asyncio
import hashlib
import html
import json
import logging
import re
import time
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser
import httpx

from . import cache, config

log = logging.getLogger("evrota.news")

NEWS_KEY = "news:items"            # sorted set: score = yayın zamanı, member = id
NEWS_ITEM = "news:item:"           # + id → JSON
SEEN_KEY = "news:seen:"            # + url hash → 1
DRAFT_KEY = "news:drafts"          # katalog taslakları (list)
KEEP_SECONDS = 14 * 86400

CATEGORIES = {"yeni_model", "fiyat", "sarj_agi", "teknoloji", "mevzuat", "genel"}

_EV_WORDS = re.compile(
    r"elektrikli|elektrik(li)? araç|\bEV\b|şarj|batarya|togg|tesla|byd|plug-?in|hibrit|"
    r"kWh|menzil|zes|eşarj|esarj|trugo|voltrun|sharz|ionity|supercharger", re.I)


def _id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _clean(text: str, limit: int = 400) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _norm_title(t: str) -> str:
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", t)          # Google News: "Başlık - Kaynak"
    return re.sub(r"[^a-z0-9çğıöşü ]", "", t.lower()).strip()


_catalog_cache: Optional[list] = None


def _catalog() -> list:
    global _catalog_cache
    if _catalog_cache is None:
        try:
            with open(config.VEHICLES_FILE, encoding="utf-8") as f:
                _catalog_cache = json.load(f).get("vehicles", [])
        except Exception:
            _catalog_cache = []
    return _catalog_cache


def match_vehicle(brand: str, model: str, title: str = "") -> Optional[str]:
    """Haberdeki aracı katalogdaki bir modelle eşleştirir (en uzun model adı eşleşmesi)."""
    text = f"{brand} {model} {title}".lower()
    best, best_len = None, 0
    for v in _catalog():
        if v["brand"].lower() not in text:
            continue
        # "T10X V2 Uzun Menzil" → "t10x"; "Model Y Long Range" → "model y"
        key = re.split(r"\s(?:v\d|uzun|standart|long|arkadan|\d+\s?kwh|pro|rwd|awd)\b", v["model"].lower())[0].strip()
        if key and key in text and len(key) > best_len:
            best, best_len = v["id"], len(key)
    return best


def load_feeds() -> list:
    try:
        with open(config.NEWS_FEEDS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data.get("feeds", []) if x.get("url") and x.get("enabled", True)]
    except Exception as e:
        log.error("Haber kaynakları okunamadı: %s", e)
        return []


async def _fetch_feed(client: httpx.AsyncClient, feed: dict) -> list:
    try:
        r = await client.get(feed["url"])
        if r.status_code != 200:
            log.warning("RSS %s → HTTP %s", feed.get("name"), r.status_code)
            return []
        parsed = feedparser.parse(r.content)
    except Exception as e:
        log.warning("RSS %s → %s", feed.get("name"), e)
        return []
    out = []
    for e in parsed.entries[:40]:
        link = e.get("link") or ""
        title = _clean(e.get("title", ""), 200)
        if not link or not title:
            continue
        published = None
        for k in ("published", "updated"):
            if e.get(k):
                try:
                    published = int(parsedate_to_datetime(e[k]).timestamp())
                    break
                except Exception:
                    pass
        source = feed.get("name", "")
        src = e.get("source")
        if isinstance(src, dict) and src.get("title"):
            source = src["title"]                         # Google News asıl yayıncıyı verir
        if feed.get("type") == "discovery":
            m = re.search(r"\s+-\s+([^-]{2,40})$", title)  # "Başlık - Kaynak"
            if m:
                if not (isinstance(src, dict) and src.get("title")):
                    source = m.group(1).strip()
                title = title[:m.start()].strip()
        out.append({
            "id": _id(link), "url": link, "title": title, "source": source,
            "published": published or int(time.time()),
            "snippet": _clean(e.get("summary", ""), 500),
            "feedType": feed.get("type", "publisher"),
        })
    return out


GEMINI_PROMPT = """Aşağıda Türkçe haber başlıkları ve kısa özetleri var. Türkiye'deki elektrikli araç (EV)
kullanıcıları için bir uygulamanın haber akışını hazırlıyorsun.

Her haber için şunu yap:
- "ev": Haber elektrikli araçlar (BEV/PHEV), şarj ağları, EV fiyatları, EV teşvik/vergi düzenlemeleri
  veya batarya teknolojisiyle doğrudan ilgiliyse true, değilse false.
- "baslik": Kısa, net Türkçe başlık (en fazla 90 karakter). Tıklama tuzağı yapma.
- "ozet": Haberi KENDİ cümlelerinle 2-3 cümlede özetle (en fazla 350 karakter). Sadece verilen
  bilgiye dayan, bilgi uydurma, kaynağın cümlelerini kopyalama.
- "neden_onemli": EV sahibi ya da almak isteyen biri için neden önemli, tek cümle (en fazla 160 karakter).
- "one_cikanlar": Haberde AÇIKÇA geçen sayısal/teknik bilgiler, en fazla 4 kısa madde
  (ör. "Menzil: 520 km (WLTP)", "DC şarj: 150 kW", "Fiyat: 2.150.000 TL", "Batarya: 88,5 kWh").
  Haberde geçmeyen bilgiyi YAZMA; yoksa boş liste.
- "kategori": yeni_model | fiyat | sarj_agi | teknoloji | mevzuat | genel
- "marka" ve "model": Haberde geçen ana araç (yoksa boş).
- "fiyat_tl": Haberde Türkiye fiyatı açıkça geçiyorsa sayı olarak (yoksa null).

SADECE şu biçimde JSON dizisi döndür, sıra girişle aynı olsun:
[{{"i": 0, "ev": true, "baslik": "", "ozet": "", "neden_onemli": "", "one_cikanlar": [], "kategori": "genel", "marka": "", "model": "", "fiyat_tl": null}}]

Haberler:
{items}"""


async def _classify_with_gemini(items: list) -> Optional[list]:
    if not config.GEMINI_API_KEY or not items:
        return None
    lines = "\n".join(f'{i}. [{it["source"]}] {it["title"]} — {it["snippet"][:300]}' for i, it in enumerate(items))
    body = {
        "contents": [{"parts": [{"text": GEMINI_PROMPT.format(items=lines)}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json", "maxOutputTokens": 8192},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
            r = await client.post(url, params={"key": config.GEMINI_API_KEY}, json=body)
        if r.status_code != 200:
            log.warning("Gemini HTTP %s: %s", r.status_code, r.text[:300])
            return None
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        return data if isinstance(data, list) else None
    except Exception as e:
        log.warning("Gemini sınıflandırma hatası: %s", e)
        return None


async def run_once() -> dict:
    """Tüm kaynakları tarar, yeni haberleri işleyip kaydeder. İstatistik döner."""
    r = cache.client()
    if r is None:
        raise RuntimeError("Redis yok, haberler kaydedilemez")
    feeds = load_feeds()
    headers = {"User-Agent": "EVrotaNewsBot/1.0 (+https://github.com/hafifbilgiler/evrota-backend)"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), headers=headers,
                                 follow_redirects=True) as client:
        results = await asyncio.gather(*[_fetch_feed(client, f) for f in feeds])
    raw = [it for lst in results for it in lst]

    # Tekrarları ele: aynı link veya aynı (normalleştirilmiş) başlık; daha önce görülenler
    fresh, titles = [], set()
    for it in sorted(raw, key=lambda x: (x["feedType"] != "publisher", -x["published"])):
        nt = _norm_title(it["title"])
        if nt in titles:
            continue
        titles.add(nt)
        if await r.exists(SEEN_KEY + it["id"]):
            continue
        if time.time() - it["published"] > KEEP_SECONDS:
            continue
        fresh.append(it)
    fresh = fresh[: config.NEWS_MAX_PER_RUN]

    stored = drafts = 0
    for i in range(0, len(fresh), 20):
        batch = fresh[i:i + 20]
        verdicts = await _classify_with_gemini(batch)
        for j, it in enumerate(batch):
            await r.set(SEEN_KEY + it["id"], "1", ex=KEEP_SECONDS)
            v = None
            if verdicts:
                v = next((x for x in verdicts if isinstance(x, dict) and x.get("i") == j), None)
            if v is not None:
                if not v.get("ev"):
                    continue
                item = {
                    "id": it["id"], "url": it["url"], "source": it["source"], "published": it["published"],
                    "title": (v.get("baslik") or it["title"])[:120],
                    "summary": (v.get("ozet") or "")[:380],
                    "whyImportant": (v.get("neden_onemli") or "")[:180],
                    "highlights": [str(h)[:60] for h in (v.get("one_cikanlar") or []) if h][:4],
                    "category": v.get("kategori") if v.get("kategori") in CATEGORIES else "genel",
                    "brand": (v.get("marka") or "")[:40], "model": (v.get("model") or "")[:60],
                    "priceTl": v.get("fiyat_tl") if isinstance(v.get("fiyat_tl"), (int, float)) else None,
                    "aiSummary": True,
                }
            else:
                if not _EV_WORDS.search(it["title"] + " " + it["snippet"]):
                    continue
                item = {"id": it["id"], "url": it["url"], "source": it["source"], "published": it["published"],
                        "title": it["title"][:120], "summary": "", "whyImportant": "", "highlights": [],
                        "category": "genel",
                        "brand": "", "model": "", "priceTl": None, "aiSummary": False}
            item["vehicleId"] = match_vehicle(item.get("brand", ""), item.get("model", ""), item["title"])
            await r.set(NEWS_ITEM + item["id"], json.dumps(item, ensure_ascii=False), ex=KEEP_SECONDS)
            await r.zadd(NEWS_KEY, {item["id"]: item["published"]})
            stored += 1
            if item["category"] in ("yeni_model", "fiyat") and item["brand"]:
                await r.lpush(DRAFT_KEY, json.dumps({"newsId": item["id"], "brand": item["brand"],
                    "model": item["model"], "priceTl": item["priceTl"], "category": item["category"],
                    "title": item["title"], "url": item["url"], "at": int(time.time())}, ensure_ascii=False))
                drafts += 1
    await r.zremrangebyscore(NEWS_KEY, 0, time.time() - KEEP_SECONDS)
    await r.ltrim(DRAFT_KEY, 0, 499)
    stats = {"feeds": len(feeds), "fetched": len(raw), "new": len(fresh), "stored": stored, "drafts": drafts,
             "ai": bool(config.GEMINI_API_KEY)}
    log.info("Haber taraması: %s", stats)
    await r.set("news:last_run", json.dumps({**stats, "at": int(time.time())}))
    return stats


async def latest(limit: int = 50, category: Optional[str] = None, before: Optional[int] = None) -> list:
    r = cache.client()
    if r is None:
        return []
    max_score = before - 1 if before else "+inf"
    ids = await r.zrevrangebyscore(NEWS_KEY, max_score, "-inf", start=0, num=limit * 2)
    out = []
    for nid in ids:
        raw = await r.get(NEWS_ITEM + nid)
        if not raw:
            continue
        item = json.loads(raw)
        if category and item.get("category") != category:
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out
