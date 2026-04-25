import os
# Suppress TensorFlow oneDNN info logs
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

import sys
import asyncio
import logging

# ---------------------------------------------------------------
# PATH SETUP: Allow imports from THIS backend folder
# ---------------------------------------------------------------
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from loguru import logger

# Silence noisy external libraries
logging.getLogger("httpx").setLevel(logging.WARNING)

from src.delivery.web_dashboard import router as dashboard_router
from src.delivery.user_retention import router as retention_router
from src.delivery.admin_portal import router as admin_router

from src.config.settings import DATA_DIR

# Configure logging
try:
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(exist_ok=True, parents=True)
    logger.add(log_dir / "app.log", rotation="500 MB", level="INFO")
except Exception as e:
    print(f"File logging disabled due to error: {e}")

from src.database.models import init_db

# GLOBAL STATE FOR MONITORING
LAST_CYCLE_RUN = "Never"
DB_TYPE = "Unknown"

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AI News Intelligence Agent (Backend API)...")
    
    init_db()
    logger.info("Database initialized.")

    from src.config.firebase_config import initialize_firebase
    initialize_firebase()

    def _background_startup_tasks():
        try:
            from seed_newspapers import seed_newspapers
            seed_newspapers()
        except Exception as e:
            logger.error(f"Newspaper seeding failed: {e}")

        try:
            from src.utils.fix_data import fix_data
            fix_data()
        except Exception as e:
            logger.error(f"Data fix failed: {e}")

        try:
            from src.database.models import SessionLocal, VerifiedNews, SystemConfig
            db_cfg = SessionLocal()
            if db_cfg.query(SystemConfig).count() == 0:
                logger.info("Initializing SystemConfig defaults...")
                defaults = [
                    SystemConfig(config_key="show_scholarships", config_value="true"),
                    SystemConfig(config_key="show_exams", config_value="true"),
                    SystemConfig(config_key="show_admissions", config_value="true"),
                    SystemConfig(config_key="show_career", config_value="true"),
                    SystemConfig(config_key="maintenance_mode", config_value="false"),
                    SystemConfig(config_key="app_version", config_value="1.0.0"),
                ]
                db_cfg.add_all(defaults)
                db_cfg.commit()
            db_cfg.close()
        except Exception as e:
            logger.error(f"SystemConfig seeding failed: {e}")

        db_news = SessionLocal()
        try:
            from src.database.models import VerifiedNews
            # If the database is empty or very old (stale), trigger a cycle immediately
            news_count = db_news.query(VerifiedNews).count()
            if news_count == 0:
                logger.info("Empty database detected. Triggering immediate 15-minute news cycle...")
                from src.scheduler.task_scheduler import run_news_cycle
                asyncio.run(run_news_cycle())
            else:
                logger.info(f"Database has {news_count} articles. Scheduler will handle updates.")
        except Exception as e:
            logger.error(f"Failed to auto-trigger news cycle: {e}")
        finally:
            db_news.close()

    import threading
    threading.Thread(target=_background_startup_tasks, daemon=True).start()

    from src.scheduler.task_scheduler import start_scheduler
    scheduler = start_scheduler()
    logger.info("Scheduler started.")

    yield

    logger.info("Shutting down...")
    if scheduler:
        scheduler.shutdown()


app = FastAPI(title="AI News Intelligence Agent - Backend API", lifespan=lifespan)

@app.get("/api/v2/system/health")
async def system_health():
    """Diagnostic endpoint to verify if the 15-minute cycle is alive."""
    from src.database.models import SessionLocal, VerifiedNews, SystemConfig
    from src.config.settings import DATABASE_URL
    import datetime
    
    db = SessionLocal()
    try:
        count = db.query(VerifiedNews).count()
        last_article = db.query(VerifiedNews).order_by(VerifiedNews.id.desc()).first()
        db_status = "Connected"
    except Exception as e:
        count = 0
        last_article = None
        db_status = f"Error: {str(e)}"
    finally:
        db.close()
        
    return {
        "status": "online",
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "database": {
            "status": db_status,
            "type": "Postgres/Supabase" if "supabase" in DATABASE_URL.lower() else "Local SQLite",
            "article_count": count,
            "last_article_id": last_article.id if last_article else None,
            "last_article_time": last_article.created_at.isoformat() if last_article and hasattr(last_article, 'created_at') else "Unknown"
        },
        "scheduler": {
            "last_run": db.query(SystemConfig).filter(SystemConfig.config_key == "last_news_cycle_run").first().config_value if db.query(SystemConfig).filter(SystemConfig.config_key == "last_news_cycle_run").first() else "Never",
            "interval": "15 Minutes"
        }
    }

# --- CORS FOR ANDROID .AAB & DECOUPLED FRONTEND ---
# Using allow_credentials=False is required by the CORS protocol when using allow_origins=["*"].
# This is perfectly fine because Firebase handles our Authentication tokens, not server cookies.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include Routers (Now only serving API and necessary Admin logic)
from src.delivery.user_retention import router_legacy, router_user
app.include_router(retention_router)
app.include_router(router_legacy)
app.include_router(router_user)
app.include_router(dashboard_router)
app.include_router(admin_router)


@app.middleware("http")
async def maintenance_middleware(request: Request, call_next):
    skip_paths = ["/api/admin", "/admin", "/static", "/health", "/favicon.ico"]
    if any(request.url.path.startswith(path) for path in skip_paths):
        return await call_next(request)
    try:
        from src.database.models import SessionLocal, SystemConfig
        db = SessionLocal()
        mode = db.query(SystemConfig).filter(SystemConfig.config_key == "maintenance_mode").first()
        is_maintenance = mode and mode.config_value.lower() == "true"
        db.close()
        if is_maintenance:
            return HTMLResponse(content="""
                <!DOCTYPE html><html><head><title>Maintenance</title></head>
                <body style="background:#020617;color:white;font-family:sans-serif;text-align:center;padding:4rem;">
                <h1>Neural Maintenance</h1>
                <p>We'll be back online within minutes.</p></body></html>
            """, status_code=503)
    except Exception as e:
        logger.error(f"Maintenance check failed: {e}")
    return await call_next(request)

# Re-enable backend static file serving specifically for user-uploaded images and generated graphics
from fastapi.staticfiles import StaticFiles

# --- PERSISTENT AUDIO SERVING ---
# We serve audio from the persistent DATA_DIR/audio folder (Railway Persistence)
from src.config.settings import DATA_DIR
audio_dir = DATA_DIR / "audio"
audio_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static/audio", StaticFiles(directory=str(audio_dir)), name="static_audio")

static_path = os.path.join(BACKEND_DIR, "static")
if os.path.exists(static_path):
    app.mount("/static", StaticFiles(directory=static_path), name="static")

# (Backend does not serve UI assets like favicon anymore)


@app.get("/health")
async def health_check():
    return {"status": "healthy", "mode": "separated-backend"}


def main():
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "run-once":
            logger.info("Running manual news cycle...")
            from src.scheduler.task_scheduler import run_news_cycle
            asyncio.run(run_news_cycle())
        elif command == "init-db":
            from src.utils.init_db import init_db
            init_db()
        else:
            logger.error(f"Unknown command: {command}")
    else:
        port = int(os.environ.get("PORT", 8000))
        logger.info(f"Launching backend server on port {port}...")
        uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
