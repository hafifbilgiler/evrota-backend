"""EVrota backend ayarları (hepsi ortam değişkeninden)."""
import os

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://evrota-redis:6379/0")

STATIONS_CACHE_TTL = int(os.getenv("STATIONS_CACHE_TTL", "180"))       # doluluk verisi: 3 dk
STATIONS_STALE_TTL = int(os.getenv("STATIONS_STALE_TTL", "86400"))     # Google'a ulaşılamazsa son sonuç
DAILY_PLACES_CAP = int(os.getenv("DAILY_PLACES_CAP", "1500"))          # günlük Google çağrısı üst sınırı

REPORT_TTL = int(os.getenv("REPORT_TTL", "21600"))                     # istasyon bildirimleri 6 saat geçerli
REPORTS_PER_DEVICE_HOUR = int(os.getenv("REPORTS_PER_DEVICE_HOUR", "20"))

DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
VEHICLES_FILE = os.getenv("VEHICLES_FILE", os.path.join(DATA_DIR, "vehicles.json"))
PRICES_FILE = os.getenv("PRICES_FILE", os.path.join(DATA_DIR, "prices.json"))

# ── Kimlik doğrulama ──
JWT_SECRET = os.getenv("EVROTA_JWT_SECRET", "")
TOKEN_TTL_DAYS = int(os.getenv("TOKEN_TTL_DAYS", "30"))
TOKENS_PER_IP_HOUR = int(os.getenv("TOKENS_PER_IP_HOUR", "10"))
REQUESTS_PER_MINUTE = int(os.getenv("REQUESTS_PER_MINUTE", "60"))
REQUESTS_PER_DAY = int(os.getenv("REQUESTS_PER_DAY", "2000"))
PLAY_INTEGRITY_REQUIRED = os.getenv("PLAY_INTEGRITY_REQUIRED", "false").lower() == "true"
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", "16384"))

# ── Haberler ──
NEWS_FEEDS_FILE = os.getenv("NEWS_FEEDS_FILE", os.path.join(DATA_DIR, "news_feeds.json"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
NEWS_MAX_PER_RUN = int(os.getenv("NEWS_MAX_PER_RUN", "60"))
