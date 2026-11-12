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
