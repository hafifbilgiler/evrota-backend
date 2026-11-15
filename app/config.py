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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")            # asistan + araç değerlendirmesi
NEWS_GEMINI_MODEL = os.getenv("NEWS_GEMINI_MODEL", GEMINI_MODEL)          # haber özetleme (ucuz model varsa env ile)
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "low")         # Gemini 3: minimal | low | medium | high


def gemini_config(model: str, temperature: float, max_tokens: int, json_mode: bool = False) -> dict:
    """
    Model sürümüne göre generationConfig.
    Gemini 2.x: thinkingBudget=0 (düşünme kapalı). Gemini 3+: thinkingLevel (env) ve
    sıcaklık varsayılan bırakılır (Google, Gemini 3'te sıcaklığı düşürmemeyi öneriyor).
    """
    cfg: dict = {"maxOutputTokens": max_tokens}
    if json_mode:
        cfg["responseMimeType"] = "application/json"
    if model.startswith("gemini-2"):
        cfg["temperature"] = temperature
        cfg["thinkingConfig"] = {"thinkingBudget": 0}
    else:
        cfg["thinkingConfig"] = {"thinkingLevel": GEMINI_THINKING_LEVEL}
    return cfg
NEWS_MAX_PER_RUN = int(os.getenv("NEWS_MAX_PER_RUN", "60"))
# PostgreSQL (kalıcı veri: profil, şarj kayıtları)
DB_HOST = os.getenv("DB_HOST", "evrota-postgres")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "evrota")
DB_NAME = os.getenv("DB_NAME", "evrota")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
# Rota planlayıcı (Routes API + rota üzerinde istasyon araması); yoksa Places anahtarı denenir
ROUTES_KEY = os.getenv("GOOGLE_ROUTES_API_KEY", "") or os.getenv("GOOGLE_PLACES_API_KEY", "")
ROUTE_DAILY_LIMIT = int(os.getenv("ROUTE_DAILY_LIMIT", "15"))
# Google ile giriş: Google Cloud'daki "Web application" OAuth istemci kimliği (gizli değil)
GOOGLE_WEB_CLIENT_ID = os.getenv("GOOGLE_WEB_CLIENT_ID", "")
PROFILE_ANALYSIS_DAILY = int(os.getenv("PROFILE_ANALYSIS_DAILY", "3"))   # cihaz başına günlük AI profil analizi

# AI asistan (POST /agent/chat)
AGENT_DAILY_LIMIT = int(os.getenv("AGENT_DAILY_LIMIT", "30"))     # cihaz başına günlük mesaj
AGENT_MAX_CHARS = int(os.getenv("AGENT_MAX_CHARS", "800"))        # mesaj başına karakter
# Asistan sağlayıcı sırası: ilk çalışan kullanılır, hata/kredi bitti olursa sıradakine geçer
AGENT_PROVIDER = os.getenv("AGENT_PROVIDER", "gemini")   # "gemini" | "deepinfra" | yedekli: "gemini,deepinfra"
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY", "")
DEEPINFRA_BASE_URL = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")
DEEPINFRA_MODEL = os.getenv("DEEPINFRA_MODEL", "deepseek-ai/DeepSeek-V4-Flash-0731")
