import os
from pathlib import Path
from dotenv import load_dotenv

# Suppress TensorFlow oneDNN info logs
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

load_dotenv()

# Project Root
BASE_DIR = Path(__file__).resolve().parent.parent.parent
# Data Directory - Priority: /app/data (HF), then project-local 'data'
DATA_DIR_ENV = os.getenv("DATA_DIR_PATH")
if DATA_DIR_ENV:
    DATA_DIR = Path(DATA_DIR_ENV)
elif Path("/app/data").exists():
    DATA_DIR = Path("/app/data")
else:
    DATA_DIR = BASE_DIR / "data"

DATA_DIR.mkdir(exist_ok=True, parents=True)

# 0. Base News Retrieval Keys
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
GNEWS_API_KEY = os.getenv("GNEWS_API_KEY")
GNEWS_API_KEY_2 = os.getenv("GNEWS_API_KEY_2")

# 1. OpenAI Pool — dynamically loads OPENAI_KEY_1 through OPENAI_KEY_20
OPENAI_API_KEYS = [v for i in range(1, 21) if (v := os.getenv(f"OPENAI_KEY_{i}"))]
OPENAI_API_KEY = OPENAI_API_KEYS[0] if OPENAI_API_KEYS else None

# 2. Groq Pool — dynamically loads GROQ_KEY_1 through GROQ_KEY_20
GROQ_API_KEYS = [v for i in range(1, 21) if (v := os.getenv(f"GROQ_KEY_{i}"))]
GROQ_API_KEY = GROQ_API_KEYS[0] if GROQ_API_KEYS else None

print(f"[BOOT] 🔑 Key Pool: {len(OPENAI_API_KEYS)} OpenAI + {len(GROQ_API_KEYS)} Groq = {len(OPENAI_API_KEYS)+len(GROQ_API_KEYS)} total keys loaded")

# Specialized Fallbacks (Maintained for legacy compatibility but effectively mapped to pools)
GROQ_KEY_TELUGU = GROQ_API_KEY
GROQ_KEY_HINDI = GROQ_API_KEY
GROQ_KEY_MALAYALAM = GROQ_API_KEY
GROQ_KEY_TAMIL = GROQ_API_KEY
GROQ_KEY_CRYSTAL_BALL = GROQ_API_KEY

# Translation Keys mapped to OpenAI pool
TRANSLATION_KEYS = OPENAI_API_KEYS


# Firebase Config
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
FIREBASE_AUTH_DOMAIN = os.getenv("FIREBASE_AUTH_DOMAIN")
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID")
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET")
FIREBASE_MESSAGING_SENDER_ID = os.getenv("FIREBASE_MESSAGING_SENDER_ID")
FIREBASE_APP_ID = os.getenv("FIREBASE_APP_ID")

# Database Configuration
_env_db_url = os.getenv("DATABASE_URL", "")
if _env_db_url.startswith("sqlite:///"):
    _db_path_part = _env_db_url.replace("sqlite:///", "")
    if not os.path.isabs(_db_path_part):
        # Strip common prefixes that might cause double-joining
        for prefix in ["app/data/", "data/"]:
            if _db_path_part.startswith(prefix):
                _db_path_part = _db_path_part.replace(prefix, "", 1)
                break
        _abs_db_path = (DATA_DIR / _db_path_part).resolve().absolute()
        DATABASE_URL = f"sqlite:///{_abs_db_path.as_posix()}"
    else:
        DATABASE_URL = _env_db_url
else:
    # Use default
    _default_db = (DATA_DIR / "news.db").resolve().absolute()
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_default_db.as_posix()}")

# Log Active Database (Safe Diagnostic)
_log_url = DATABASE_URL
if "@" in _log_url:
    _log_url = f"{_log_url.split('://')[0]}://****@{_log_url.split('@')[-1]}"
print(f"[BOOT] 📁 DATA_DIR: {DATA_DIR.resolve().absolute()}")
print(f"[BOOT] 🚀 DATABASE: {_log_url}")

VECTOR_DB_PATH = DATA_DIR / "vector_store.index"

# News Setting
NEWS_SOURCES_RSS = [
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"
]

# Web Setting
PORT = int(os.getenv("PORT", 7860))
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "06:00")
MIN_CREDIBILITY_SCORE = 0.4  # Lowered from 0.6 for better density
SIMILARITY_THRESHOLD = 0.85

# NewsData.io Settings
NEWSDATA_STUDENT_API_KEY = os.getenv("NEWSDATA_STUDENT_API_KEY")

# Admin Credentials — MUST be set via environment variables (no insecure defaults)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@uniintel.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")  # No default - must be set in .env
if not ADMIN_PASSWORD:
    import secrets
    ADMIN_PASSWORD = secrets.token_hex(16)  # Generate random password if not set
    import sys
    print(f"[WARNING] ADMIN_PASSWORD not set in .env! Generated temporary password: {ADMIN_PASSWORD}", file=sys.stderr)

# Admin JWT Secret — generates a random one if not set, but should be fixed in production
ADMIN_JWT_SECRET = os.getenv("ADMIN_JWT_SECRET", secrets.token_hex(32) if 'secrets' in dir() else "change_me_in_env")
