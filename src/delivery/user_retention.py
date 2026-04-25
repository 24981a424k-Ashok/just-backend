from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy import String, cast
from sqlalchemy.orm import Session
from src.database.models import SessionLocal, User, Folder, SavedArticle, ReadHistory, VerifiedNews, TopicTracking
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timedelta
import logging
logger = logging.getLogger(__name__)

router = APIRouter(tags=["Retention"], prefix="/api/v2/retention")
router_legacy = APIRouter(tags=["Retention Legacy"], prefix="/api/retention")
router_user = APIRouter(tags=["User"], prefix="/api/user")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/status")
async def get_user_status(firebase_uid: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
    if not user:
        # Create user if missing (first login)
        user = User(firebase_uid=firebase_uid)
        db.add(user)
        db.commit()
        db.refresh(user)
    
    # Calculate history map for calendar
    history = db.query(ReadHistory).filter(ReadHistory.user_id == user.id).all()
    history_map = {h.read_at.date().isoformat(): True for h in history}
    
    return {
        "status": "success",
        "current_streak": user.current_streak,
        "best_streak": getattr(user, 'best_streak', user.current_streak),
        "phone": user.phone,
        "history": history_map
    }

@router.post("/ping_streak")
async def ping_streak(payload: dict = Body(...), db: Session = Depends(get_db)):
    firebase_uid = payload.get("firebase_uid")
    if not firebase_uid:
        raise HTTPException(status_code=422, detail="firebase_uid required")
        
    user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
    if not user:
        user = User(firebase_uid=firebase_uid)
        db.add(user)
        db.commit()
        db.refresh(user)

    today = datetime.utcnow().date()
    last_active = user.last_active_date.date() if user.last_active_date else None
    
    milestone_hit = None
    if not last_active:
        user.current_streak = 1
    elif last_active == today:
        pass # Already active
    elif last_active == today - timedelta(days=1):
        user.current_streak += 1
        # Simple Milestone Logic
        if user.current_streak in [7, 30, 100]:
            milestone_hit = user.current_streak
    else:
        user.current_streak = 1
        
    user.last_active_date = datetime.utcnow()
    db.commit()
    
    return {
        "status": "success", 
        "current_streak": user.current_streak,
        "milestone_hit": milestone_hit
    }

class SaveRequest(BaseModel):
    firebase_uid: str
    news_id: int
    folder_id: Optional[int] = None

class FolderRequest(BaseModel):
    firebase_uid: str
    name: str

class HistoryRequest(BaseModel):
    firebase_uid: str
    news_id: int

@router.post("/save")
async def save_article(payload: SaveRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.firebase_uid == payload.firebase_uid).first()
    if not user:
        logger.warning(f"User {payload.firebase_uid} not found during save. Creating lazy entry.")
        user = User(firebase_uid=payload.firebase_uid)
        db.add(user)
        db.commit()
        db.refresh(user)
    
    # Check if already saved
    existing = db.query(SavedArticle).filter(
        SavedArticle.user_id == user.id,
        SavedArticle.news_id == payload.news_id
    ).first()
    
    if existing:
        return {"status": "already_saved", "message": "Article already in saves"}
    
    save_entry = SavedArticle(
        user_id=user.id,
        news_id=payload.news_id,
        folder_id=payload.folder_id
    )
    db.add(save_entry)
    db.commit()
    return {"status": "success", "message": "Article saved"}

@router.post("/history")
async def track_history(payload: HistoryRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.firebase_uid == payload.firebase_uid).first()
    if not user:
        # Lazy creation of user if they exist in Firebase but not in our DB
        logger.warning(f"User {payload.firebase_uid} not found during history track. Creating lazy entry.")
        user = User(firebase_uid=payload.firebase_uid)
        db.add(user)
        db.commit()
        db.refresh(user)
    
    # Track the reading event - but only once per article per day for streak
    existing_history = db.query(ReadHistory).filter(
        ReadHistory.user_id == user.id,
        ReadHistory.news_id == payload.news_id,
        ReadHistory.read_at >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    ).first()

    if not existing_history:
        history_entry = ReadHistory(
            user_id=user.id,
            news_id=payload.news_id
        )
        db.add(history_entry)
        
        # --- STREAK LOGIC ---
        today = datetime.utcnow().date()
        last_active = user.last_active_date.date() if user.last_active_date else None
        
        if not last_active:
             user.current_streak = 1
        elif last_active == today:
             # Already active today, streak stays
             pass
        elif last_active == today - timedelta(days=1):
             # Consecutive day, increment streak
             user.current_streak += 1
             logger.info(f"Streak Increased! User {user.id} now on {user.current_streak} days.")
        else:
             # Streak broken, reset
             user.current_streak = 1
             logger.info(f"Streak Broken. User {user.id} reset to 1.")
        
        user.last_active_date = datetime.utcnow()
    
    db.commit()
    return {"status": "success", "message": "History tracked", "streak": user.current_streak}

@router.get("/saved/{firebase_uid}")
async def get_saved_articles(firebase_uid: str, db: Session = Depends(get_db)):
    return await _fetch_saves(firebase_uid, db)

@router.get("/saved-articles")
async def get_saved_articles_alias(uid: str, db: Session = Depends(get_db)):
    """Compatibility alias for frontend calling with ?uid=... instead of path param."""
    return await _fetch_saves(uid, db)

async def _fetch_saves(firebase_uid: str, db: Session):
    user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
    if not user:
        # Instead of 404, return empty list for graceful UI
        return {"status": "success", "articles": []}
    
    saves = db.query(SavedArticle).filter(SavedArticle.user_id == user.id).all()
    result = []
    for s in saves:
        news = s.news
        if not news:
            continue
        result.append({
            "id": news.id,
            "title": news.title,
            "source": news.raw_news.source_name if news.raw_news else "Unknown",
            "category": news.category,
            "saved_at": s.saved_at.isoformat(),
            "url": news.raw_news.url if news.raw_news else "#"
        })
    return {"status": "success", "articles": result}

@router.get("/history/{firebase_uid}")
async def get_history(firebase_uid: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    history = db.query(ReadHistory).filter(ReadHistory.user_id == user.id).order_by(ReadHistory.read_at.desc()).all()
    result = []
    for h in history:
        news = h.news
        if not news:
            continue
        result.append({
            "id": news.id,
            "title": news.title,
            "source": news.raw_news.source_name if news.raw_news else "Unknown",
            "read_at": h.read_at.isoformat(),
            "url": news.raw_news.url if news.raw_news else "#"
        })
    return result

@router.delete("/history/{firebase_uid}")
async def clear_history(firebase_uid: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    db.query(ReadHistory).filter(ReadHistory.user_id == user.id).delete()
    db.commit()
    return {"status": "success", "message": "History cleared"}

@router.delete("/saved/{firebase_uid}")
async def clear_saved_articles(firebase_uid: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    db.query(SavedArticle).filter(SavedArticle.user_id == user.id).delete()
    db.commit()
    return {"status": "success", "message": "Saved articles cleared"}

@router.post("/folders")
async def create_folder(payload: FolderRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.firebase_uid == payload.firebase_uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    folder = Folder(user_id=user.id, name=payload.name)
    db.add(folder)
    db.commit()
    return {"status": "success", "folder_id": folder.id}

class PhoneUpdateRequest(BaseModel):
    firebase_uid: str
    phone: str

class TrackTopicRequest(BaseModel):
    article_id: str
    firebase_uid: str
    language: str = "english"

@router.post("/update_phone")
async def update_phone(payload: PhoneUpdateRequest, db: Session = Depends(get_db)):
    """Update user's phone number for SMS tracking."""
    user = db.query(User).filter(User.firebase_uid == payload.firebase_uid).first()
    if not user:
        user = User(firebase_uid=payload.firebase_uid, phone=payload.phone)
        db.add(user)
    else:
        user.phone = payload.phone
    db.commit()
    return {"status": "success", "message": "Phone number updated"}

@router.post("/track_topic")
async def track_topic(payload: TrackTopicRequest, db: Session = Depends(get_db)):
    """Permanent topic tracking for 30 days via SMS."""
    from src.delivery.notifications import NotificationManager
    
    # Ensure user exists
    user = db.query(User).filter(User.firebase_uid == payload.firebase_uid).first()
    if not user:
        user = User(firebase_uid=payload.firebase_uid)
        db.add(user)
        db.commit()
        db.refresh(user)
        
    # NEW: Check if phone number exists for SMS tracking
    if not user.phone:
        return {"status": "NEED_PHONE", "message": "Phone number required for SMS alerts."}
        
    # Get article keywords
    article_id_clean = payload.article_id
    if isinstance(article_id_clean, str) and article_id_clean.startswith("raw-"):
        # For raw IDs, we track by title since they aren't in VerifiedNews yet
        try:
            from src.database.models import RawNews
            raw_id = int(article_id_clean.replace("raw-", ""))
            raw = db.query(RawNews).filter(RawNews.id == raw_id).first()
            article_title = raw.title if raw else f"Topic {payload.article_id}"
            keywords = [article_title]
            news_id_to_store = None # Cannot link foreign key to RawNews in Verified-only table yet
        except:
            article_title = f"Topic {payload.article_id}"
            keywords = [article_title]
            news_id_to_store = None
    else:
        # Standard verified article tracking
        try:
            artic_id_int = int(payload.article_id)
            article = db.query(VerifiedNews).filter(VerifiedNews.id == artic_id_int).first()
            keywords = article.impact_tags or [article.category or "General Intelligence"] if article else [f"Topic {payload.article_id}"]
            article_title = article.title if article else f"Topic {payload.article_id}"
            news_id_to_store = artic_id_int
        except:
            article_title = f"Topic {payload.article_id}"
            keywords = [article_title]
            news_id_to_store = None
    
    # Check if already tracking this article to avoid duplicates
    # Cross-DB safe check for keywords in JSON array
    existing = None
    if keywords:
        existing = db.query(TopicTracking).filter(
            TopicTracking.user_id == user.id,
            cast(TopicTracking.topic_keywords, String).contains(keywords[0])
        ).first()
    
    if not existing:
        track = TopicTracking(
            user_id=user.id,
            news_id=news_id_to_store,
            topic_keywords=keywords,
            language=payload.language,
            notify_sms=True
        )
        db.add(track)
        db.commit()
    
    # Send SMS Confirmation if phone exists
    if user.phone:
        article_title_final = article_title if 'article_title' in locals() else f"Intelligence on {keywords[0]}"
        NotificationManager.send_sms(
            user.phone, 
            f"📡 AI AGENT: Now tracking '{article_title_final}'. You will receive SMS alerts for related intelligence updates over the next 30 days."
        )

    return {"status": "success", "message": "Topic tracked for 30 days"}

# --- USER COMPATIBILITY ROUTES ---

@router_user.get("/read-history")
async def get_history_compat(uid: str, db: Session = Depends(get_db)):
    """Frontend expects /api/user/read-history?uid=..."""
    user = db.query(User).filter(User.firebase_uid == uid).first()
    if not user:
        return {"status": "success", "history": []}
    
    history = db.query(ReadHistory).filter(ReadHistory.user_id == user.id).order_by(ReadHistory.read_at.desc()).limit(50).all()
    result = []
    for h in history:
        news = h.news
        if not news: continue
        result.append({
            "id": news.id,
            "title": news.title,
            "source": news.raw_news.source_name if news.raw_news else "Unknown",
            "read_at": h.read_at.isoformat(),
            "url": news.raw_news.url if news.raw_news else "#"
        })
    return {"status": "success", "history": result}

@router_user.get("/saved-articles")
async def get_saved_articles_compat(uid: str, db: Session = Depends(get_db)):
    """Frontend expects /api/user/saved-articles?uid=..."""
    return await _fetch_saves(uid, db)

@router.get("/streak_dashboard")
async def get_streak_dashboard(firebase_uid: str, db: Session = Depends(get_db)):
    """Detailed streak data for the dashboard UI."""
    user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
    if not user:
        return {"status": "error", "message": "User not found"}
        
    # Rules & Milestones
    rules = [
        "Read at least 1 verified article daily to maintain your streak.",
        "Streaks are calculated in UTC time.",
        "Unlock badges at 7, 30, and 100 days of consistency.",
        "Tracked topics count towards your daily activity."
    ]
    
    # Activity map for calendar
    history = db.query(ReadHistory).filter(ReadHistory.user_id == user.id).all()
    activity_map = {h.read_at.date().isoformat(): True for h in history}
    
    return {
        "status": "success",
        "current_streak": user.current_streak,
        "best_streak": getattr(user, 'best_streak', user.current_streak),
        "rules": rules,
        "activity": activity_map,
        "last_active": user.last_active_date.isoformat() if user.last_active_date else None
    }
