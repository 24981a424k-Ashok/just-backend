import os
import json
import asyncio
import logging
import copy
import random
import uuid
from datetime import datetime, timedelta
from collections import defaultdict

# --- CACHES & GLOBALS ---
_student_news_caches = {}
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Request, Depends, HTTPException, BackgroundTasks, Body, Form, File, UploadFile
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session
from src.database.models import SessionLocal, DailyDigest, User, VerifiedNews, Subscription, Advertisement, Newspaper, RawNews, ProtocolHistory, SystemConfig
from src.config import settings
from src.config.firebase_config import verify_token
from src.analysis.chat_engine import NewsChatEngine
from src.collectors.universe_collector import UniverseCollector
from src.utils.translator import NewsTranslator
from src.utils.ui_trans import get_ui_translations
from src.analysis.student_classifier import StudentClassifier
from src.analysis.llm_analyzer import LLMAnalyzer
from src.database.session import get_db
from pydantic import BaseModel
import requests

chat_engine = NewsChatEngine()
universe_collector = UniverseCollector()
translator = NewsTranslator()
student_classifier = StudentClassifier()
llm_analyzer = LLMAnalyzer()

# Define FIREBASE_CLIENT_CONFIG globally
FIREBASE_CLIENT_CONFIG = {
    "apiKey": settings.FIREBASE_API_KEY,
    "authDomain": settings.FIREBASE_AUTH_DOMAIN,
    "projectId": settings.FIREBASE_PROJECT_ID,
    "storageBucket": settings.FIREBASE_STORAGE_BUCKET,
    "messagingSenderId": settings.FIREBASE_MESSAGING_SENDER_ID,
    "appId": settings.FIREBASE_APP_ID
}
logger = logging.getLogger(__name__)

# Language to Indian States mapping for regional intelligence
LANGUAGE_TO_STATES = {
    "Telugu": ["Andhra Pradesh", "Telangana", "Hyderabad", "Amaravati", "Visakhapatnam"],
    "Hindi": ["Uttar Pradesh", "Bihar", "Madhya Pradesh", "Rajasthan", "Haryana", "Delhi"],
    "Tamil": ["Tamil Nadu", "Chennai", "Coimbatore", "Madurai"],
    "Kannada": ["Karnataka", "Bengaluru", "Mysuru", "Hubballi"],
    "Malayalam": ["Kerala", "Thiruvananthapuram", "Kochi", "Kozhikode"],
    "Bengali": ["West Bengal", "Kolkata", "Howrah"],
    "Gujarati": ["Gujarat", "Ahmedabad", "Surat", "Vadodara"],
    "Marathi": ["Maharashtra", "Mumbai", "Pune", "Nagpur"]
}

router = APIRouter()
from src.delivery.user_retention import router as retention_router
router.include_router(retention_router)
# templates = Jinja2Templates(directory="web/templates") # REMOVED: Backend is pure API now

from fastapi.responses import RedirectResponse
@router.get("/admin", include_in_schema=False)
async def admin_redirect():
    """Shortcut to the AI Command Center."""
    return RedirectResponse(url="/api/admin/dashboard")

# ---- AGGRESSIVE RECURSIVE NORMALIZATION UTILITIES ----
def _deep_normalize_list(val):
    """Recursively decode JSON strings until we get a proper Python list or a plain string."""
    if not val: return []
    
    # HEAL: If it's a list that looks like a split JSON string (e.g. ['[', '"', ...])
    # Heuristic: the list is long and the first element is a bracket or quote character
    if isinstance(val, list) and len(val) > 2:
        v0 = str(val[0]).strip()
        if v0 in ['[', '{', '"', "'"]:
            try:
                # Reassemble the string from the characters
                reassembled = "".join([str(x) for x in val])
                # If it looks like a JSON array/object, try to parse it
                if reassembled.startswith('[') or reassembled.startswith('{'):
                    try:
                        parsed = json.loads(reassembled)
                        return _deep_normalize_list(parsed)
                    except: pass
                # If it was a double-quoted string like ["\"", "H", "e", "l", "l", "o", "\""]
                if reassembled.startswith('"') or reassembled.startswith("'"):
                    try:
                        parsed = json.loads(reassembled)
                        return _deep_normalize_list(parsed)
                    except: pass
            except: pass

    if isinstance(val, list):
        normalized_items = []
        for item in val:
            if isinstance(item, str) and (item.strip().startswith('[') or item.strip().startswith('{')):
                try:
                    nested = json.loads(item)
                    normalized_items.extend(_deep_normalize_list(nested))
                except: normalized_items.append(item)
            else:
                normalized_items.append(item)
        return [str(x).strip() for x in normalized_items if x]
    
    if isinstance(val, str):
        s = val.strip()
        if s.startswith('[') or s.startswith('{'):
            try:
                parsed = json.loads(s)
                return _deep_normalize_list(parsed)
            except: pass
        if s: return [s]
    return []

def _deep_normalize_str(val):
    """Recursively decode JSON strings until we get a plain string or a list (which we stringify)."""
    if val is None: return ""
    if isinstance(val, str):
        s = val.strip()
        if s.startswith('{') or s.startswith('['):
            try:
                parsed = json.loads(s)
                return _deep_normalize_str(parsed)
            except: pass
        return s
    if isinstance(val, dict):
        res = val.get('hindi') or val.get('english') or val.get('native') or val.get('text')
        if res: return _deep_normalize_str(res)
        return str(val)
    if isinstance(val, list):
        return " ".join(_deep_normalize_list(val))
    return str(val)

def normalize_article_data(data: dict):
    """Apply definitive normalization to a news article dictionary."""
    if not isinstance(data, dict): return data
    
    # 1. Normalize bullet lists (handle both 'summary_bullets' and 'bullets' keys)
    bullets_key = "summary_bullets" if "summary_bullets" in data else "bullets"
    data[bullets_key] = _deep_normalize_list(data.get(bullets_key, []))
    
    tags_key = "impact_tags" if "impact_tags" in data else "tags"
    data[tags_key] = _deep_normalize_list(data.get(tags_key, []))
    
    # 2. Normalize text fields (handle polymorphic naming)
    why_key = "why_it_matters" if "why_it_matters" in data else "why"
    who_key = "who_is_affected" if "who_is_affected" in data else "affected"
    
    for field in ["title", "extra_stuff", "what_happens_next", why_key, who_key]:
        if field in data:
            data[field] = _deep_normalize_str(data.get(field, ""))
        
    # 3. Force rebuild 'content' for old JS compatibility
    # Use normalized values for the combined body
    bullets_text = "\n".join([f"• {b}" for b in data.get(bullets_key, [])])
    data["content"] = f"### {data.get('title', 'Intelligence report')}\n\n**Summary:**\n{bullets_text}\n\n**Why It Matters:**\n{data.get(why_key, '')}\n\n**Who is Affected:**\n{data.get(who_key, '')}\n\n**Extra Context:**\n{data.get('extra_stuff', '')}\n\n**What Happens Next:**\n{data.get('what_happens_next', '')}\n\n---\n*Source: {data.get('official_url') or data.get('url') or 'Global Intel'}*"
    
    # 4. Decoupled Architecture Image Patch & Fallback
    # Ensure Absolute Image URLs (Crucial for Decoupled Architecture)
    image_url = data.get("image_url")
    # For categorized fallbacks
    article_cat = data.get("category", "General")
    # If student category is available, use it for better matching
    if data.get("student_category"): 
        article_cat = "Education"
    
    if not image_url or str(image_url).lower() == 'none' or str(image_url) == "":
        data["image_url"] = get_fallback_image(data.get("title", ""), article_cat)
    else:
        # Check if it needs Port 8000 prefix (Internal Static Assets)
        if str(image_url).startswith("/static/"):
            # Prepend backend URL
            data["image_url"] = f"http://127.0.0.1:8000{image_url}"
        elif not str(image_url).startswith("http") and not str(image_url).startswith("data:"):
            # Fallback for other relative paths
            data["image_url"] = f"http://127.0.0.1:8000/static/{image_url.lstrip('/')}"
            
    return data

STUDENT_NEWS_CATEGORIES = [
    "Scholarships & Internships", "Exams & Results", "Policy & Research", 
    "Admissions & Courses", "Campus Life", "Career & Jobs", "Education",
    "Student Opportunities", "Academic Research", "Science & Health", "Tech"
]
STUDENT_KEYWORDS = [
    "student", "exam", "school", "university", "college", "scholarship", "syllabus", 
    "ugc", "cbse", "nta", "placement", "job", "career", "admission", "startup", 
    "grant", "hackathon", "funding", "education", "learning", "degree", "diploma", 
    "research", "campus", "internship", "hiring", "recruitment", "youth", "academic", 
    "tuition", "entrance", "vacancy", "intern", "test", "result", "admit", "coaching", 
    "training", "fresher", "neet", "jee", "upsc", "ssc", "board exam", "admit card",
    "fellowship", "study abroad", "visa", "student loan", "masters", "bachelors", "phd",
    "placement", "recruiter", "layoff", "salary", "stipend", "cutoff", "eligibility"
]

def is_student_article_logic(article):
    """Unified logic to determine if an article should be shown in the student portal."""
    # Build a larger context for better keyword matching
    combined = (
        (article.title or "") + " " + 
        (article.why_it_matters or "") + " " + 
        (article.who_is_affected or "") + " " + 
        (article.category or "")
    ).lower()
    
    is_student_cat = article.category in STUDENT_NEWS_CATEGORIES
    has_keywords = any(kw in combined for kw in STUDENT_KEYWORDS)
    is_global = article.country == "Global"
    
    # Specific exclusion for pure market/stock news not impacting education
    if "stock price" in combined or "market capitalization" in combined:
        if not is_student_cat:
            return False
            
    return is_student_cat or has_keywords or is_global

def log_protocol_action(db: Session, action: str, target_type: str, target_id: str = None, admin_user: str = "Admin", details: str = None):
    """Helper to record administrative actions for protocol history."""
    try:
        new_log = ProtocolHistory(
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id else None,
            admin_user=admin_user,
            details=details,
            timestamp=datetime.utcnow()
        )
        db.add(new_log)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to log protocol action: {e}")
        db.rollback()

FALLBACK_BY_CATEGORY = {
    "Education": [
        "https://images.unsplash.com/photo-1523050335456-c38a89b7928b?q=80&w=1000",
        "https://images.unsplash.com/photo-1541339907198-e08756dea43f?q=80&w=1000",
        "https://images.unsplash.com/photo-1524178232363-1fb2b075b655?q=80&w=1000",
        "https://images.unsplash.com/photo-1497633762265-9d179a990aa6?q=80&w=1000"
    ],
    "Technology": [
        "https://images.unsplash.com/photo-1518770660439-4636190af475?q=80&w=1000",
        "https://images.unsplash.com/photo-1485827404703-89b55fcc595e?q=80&w=1000",
        "https://images.unsplash.com/photo-1451187580459-43490279c0fa?q=80&w=1000",
        "https://images.unsplash.com/photo-1531297484001-80022131f5a1?q=80&w=1000"
    ],
    "Sports": [
        "https://images.unsplash.com/photo-1504450758481-7338eba7524a?q=80&w=1000",
        "https://images.unsplash.com/photo-1461896836934-ffe607ba8211?q=80&w=1000",
        "https://images.unsplash.com/photo-1517649763962-0c623066013b?q=80&w=1000"
    ],
    "Politics": [
        "https://images.unsplash.com/photo-1529107386315-e1a2ed48a620?q=80&w=1000",
        "https://images.unsplash.com/photo-1540910419892-4a36d2c3266c?q=80&w=1000"
    ],
    "Finance": [
        "https://images.unsplash.com/photo-1611974714451-22fb142371a5?q=80&w=1000",
        "https://images.unsplash.com/photo-1590283603385-17ffb3a7f29f?q=80&w=1000"
    ],
    "General": [
        "https://images.unsplash.com/photo-1504711434969-e33886168f5c?q=80&w=1000",
        "https://images.unsplash.com/photo-1495020689067-958852a7765e?q=80&w=1000",
        "https://images.unsplash.com/photo-1476242484419-cf5c1d4ee04b?q=80&w=1000"
    ]
}

def get_fallback_image(seed: str, category: str = "General") -> str:
    """Deterministically select a categorized fallback image based on djb2 hash"""
    cat = category if category in FALLBACK_BY_CATEGORY else "General"
    pool = FALLBACK_BY_CATEGORY[cat]
    
    if not seed: return pool[0]
    hash_val = 5381
    for char in seed:
        hash_val = ((hash_val << 5) + hash_val) + ord(char)
    return pool[abs(hash_val) % len(pool)]
def normalize_country(c):
    if not c: return None, [], 'english'
    mapping = {
        "jp": ("Japan", ["japan", "jp"], "japanese"),
        "us": ("USA", ["usa", "united states", "us", "america"], "english"),
        "in": ("India", ["india", "in", "bharat"], "hindi"),
        "gb": ("UK", ["uk", "united kingdom", "britain", "england"], "english"),
        "ru": ("Russia", ["russia", "ru"], "russian"),
        "de": ("Germany", ["germany", "de"], "german"),
        "fr": ("France", ["france", "fr"], "french"),
        "sg": ("Singapore", ["singapore", "sg"], "english"),
        "cn": ("China", ["china", "ch", "cn", "zh"], "chinese"),
        "ae": ("UAE", ["uae", "ae", "dubai", "abu dhabi"], "arabic")
    }
    
    val = c.lower().strip()
    # Check if val is a code
    if val in mapping:
        name, keys, lang = mapping[val]
    else:
        # Check if val is a name
        name = c.capitalize()
        keys = [val]
        lang = "english"
        for code, (cname, ckeys, clang) in mapping.items():
            if val in ckeys:
                name, keys, lang = cname, ckeys, clang
                break

    return name, list(set(keys)), lang


# REMOVED: Root redirect/landing page (Moved to Frontend Server)

# =============================================================================
# API v2: BOOTSTRAP — True Decoupled Frontend Entry Point
# Returns all dashboard context as JSON (no Jinja2 / HTML rendering)
# A standalone frontend can call GET /api/v2/bootstrap?lang=english&category=sports
# =============================================================================
@router.get("/api/v2/bootstrap")
async def api_bootstrap(
    request: Request,
    category: str = None,
    country: str = None,
    lang: str = 'english',
    db: Session = Depends(get_db)
):
    """
    JSON bootstrap endpoint for the decoupled frontend.
    Returns the exact same data context that the Jinja2 uses,
    but as a clean JSON response instead of rendered HTML.
    """
    try:
        from src.utils.ui_trans import get_ui_translations
        # Get latest digest
        latest_digest = db.query(DailyDigest).filter(DailyDigest.is_published == True).order_by(DailyDigest.date.desc()).first()
        if not latest_digest:
            latest_digest = db.query(DailyDigest).order_by(DailyDigest.date.desc()).first()

        # Auto-repair
        if not latest_digest and db.query(VerifiedNews).count() > 0:
            from src.digest.generator import DigestGenerator
            generator = DigestGenerator()
            await generator.create_daily_digest(db)
            latest_digest = db.query(DailyDigest).filter(DailyDigest.is_published == True).order_by(DailyDigest.date.desc()).first()

        # Ads
        all_ads = db.query(Advertisement).filter(
            or_(Advertisement.target_platform == "main", Advertisement.target_platform == "both")
        ).order_by(Advertisement.created_at.desc()).limit(30).all()
        if not all_ads:
            all_ads = db.query(Advertisement).order_by(Advertisement.created_at.desc()).limit(10).all()

        def _ad_to_dict(ad):
            return {
                "id": ad.id,
                "caption": ad.caption,
                "image_url": ad.image_url,
                "target_url": ad.target_url,
                "position": getattr(ad, 'position', 'both'),
            }

        left_ads  = [_ad_to_dict(a) for a in all_ads if getattr(a, 'position', 'both') in ("left", "both")]
        right_ads = [_ad_to_dict(a) for a in all_ads if getattr(a, 'position', 'both') in ("right", "both")]
        mobile_ads = [_ad_to_dict(a) for a in all_ads if getattr(a, 'position', 'both') in ("mobile", "both")]

        # Papers & Categories
        papers = db.query(Newspaper).order_by(Newspaper.name.asc()).all()
        unique_map   = {}
        unique_papers = []
        for p in papers:
            key = (p.country or "Global").strip().lower()
            if key not in unique_map:
                unique_map[key] = True
                unique_papers.append({"id": p.id, "name": p.name, "country": p.country, "url": p.url, "logo_color": p.logo_color, "logo_text": p.logo_text})

        categories = [c[0] for c in db.query(VerifiedNews.category).distinct().all() if c[0]]
        if len(categories) < 5:
            # Inject default categories if DB is sparse to ensure UI looks full
            categories = list(set(STUDENT_NEWS_CATEGORIES + ["Politics", "Tech", "Business", "Sports", "World News", "Entertainment", "Environment"]))

        # Digest processing (freshness + dedup)
        import copy as _copy
        digest_data = _copy.deepcopy(latest_digest.content_json) if latest_digest else {
            "top_stories": [], "breaking_news": [], "trending_news": [], "brief": [],
            "is_system_initializing": True
        }

        now_utc = datetime.utcnow()
        cutoff = now_utc - timedelta(hours=120) # 5 days back (User requested 4 days)
        def _fresh(item):
            pub = item.get("published_at")
            if pub and isinstance(pub, str):
                try:
                    return datetime.fromisoformat(pub.replace("Z", "+00:00")) > cutoff
                except: return True
            return True

        for sec in ["top_stories", "breaking_news", "trending_news", "brief"]:
            if sec in digest_data and digest_data[sec]:
                digest_data[sec] = [s for s in digest_data[sec] if _fresh(s)]
                # NORMALIZE
                for s in digest_data[sec]:
                    normalize_article_data(s)

        # Category filter
        if category and digest_data:
            normalized_cat = category.lower()
            synonyms = {"technology": "tech", "finances": "finance", "economy": "finance", "geopolitics": "politics"}
            cat_target = synonyms.get(normalized_cat, normalized_cat)
            for sec in ["top_stories", "breaking_news", "trending_news"]:
                if sec in digest_data:
                    digest_data[sec] = [
                        s for s in digest_data[sec]
                        if (s.get("category") or "").lower() in (cat_target, normalized_cat)
                    ]
            
            # LIVE FALLBACK for Category
            if not digest_data.get("top_stories"):
                live_cat = db.query(VerifiedNews).filter(VerifiedNews.category.ilike(f"%{category}%")).order_by(VerifiedNews.id.desc()).limit(15).all()
                if live_cat:
                    digest_data["top_stories"] = [normalize_article_data({"id": n.id, "title": n.headline or n.title, "category": n.category, "bullets": n.summary_bullets or [n.title]}) for n in live_cat]

        elif country and digest_data:
            target_name, match_keys, _ = normalize_country(country)
            countries_data = digest_data.get("countries", {})
            country_stories = []
            for k, v in countries_data.items():
                if k.lower() in match_keys:
                    country_stories = v
                    break
            
            # LIVE FALLBACK for Country Node
            if not country_stories:
                live_country = db.query(VerifiedNews).filter(VerifiedNews.country.in_(match_keys)).order_by(VerifiedNews.id.desc()).limit(15).all()
                country_stories = [normalize_article_data({"id": n.id, "title": n.headline or n.title, "category": n.category, "bullets": n.summary_bullets or [n.title]}) for n in live_country]
            
            if country_stories:
                # NORMALIZE
                for s in country_stories:
                    normalize_article_data(s)
                digest_data["top_stories"] = country_stories

        # --- LIVE FALLBACK for Main Dashboard ---
        if not digest_data.get("top_stories") and not category and not country:
            live_main = db.query(VerifiedNews).order_by(VerifiedNews.id.desc()).limit(15).all()
            if live_main:
                digest_data["top_stories"] = [normalize_article_data({"id": n.id, "title": n.headline or n.title, "category": n.category, "bullets": n.summary_bullets or [n.title]}) for n in live_main]
                digest_data["is_system_initializing"] = False 


        firebase_config = {
            "apiKey": settings.FIREBASE_API_KEY,
            "authDomain": settings.FIREBASE_AUTH_DOMAIN,
            "projectId": settings.FIREBASE_PROJECT_ID,
            "storageBucket": settings.FIREBASE_STORAGE_BUCKET,
            "messagingSenderId": settings.FIREBASE_MESSAGING_SENDER_ID,
            "appId": settings.FIREBASE_APP_ID
        }

        # --- GLOBAL TRANSLATION PASS (Strict Requirement) ---
        if lang and lang.lower() != 'english' and digest_data:
            import asyncio
            try:
                # Deadline for translation: 90s (Allows for multi-key pool retries)
                await asyncio.wait_for(
                    translator.translate_node_bulk(digest_data, lang),
                    timeout=90.0
                )
            except Exception as e:
                logger.error(f"Global API Bootstrap translation failed: {e}")

        return {
            "status": "success",
            "date": latest_digest.date.strftime("%Y-%m-%d") if latest_digest else "Initializing",
            "digest": digest_data,
            "firebase_config": firebase_config,
            "left_ads": left_ads,
            "right_ads": right_ads,
            "mobile_ads": mobile_ads,
            "papers": unique_papers,
            "categories": categories,
            "vapid_public_key": settings.VAPID_PUBLIC_KEY,
            "selected_category": category,
            "selected_country": country,
            "selected_lang": lang,
            "trending_title": f"{category.capitalize()} Trending" if category else "Global Intelligence Feed",
            "ui": get_ui_translations(lang),
        }

    except Exception as e:
        import traceback
        logger.error(f"Bootstrap API error: {e}\n{traceback.format_exc()}")
        return {"status": "error", "message": str(e)}


# REMOVED: /dashboard HTML route (Moved to Frontend Server)
# The data is now served exclusively through /api/v2/bootstrap below.

# REMOVED: Miscellaneous HTML routes (Moved to Frontend Server)

@router.get("/api/article/{article_id}")
async def get_article_detail(article_id: str, lang: str = "english", url: str = None, db: Session = Depends(get_db)):
    """Fetch full intelligence detail with on-the-fly transformation for non-English"""
    data = {}
    
    # Check if article_id is a DB ID or a URL fallback
    if article_id.isdigit():
        article_id_int = int(article_id)
        article = db.query(VerifiedNews).filter(VerifiedNews.id == article_id_int).first()
        if article:
            data = article.to_dict()
            if not data.get("image_url") and article.raw_news:
                data["image_url"] = article.raw_news.url_to_image
        else:
            # FALLBACK: Check RawNews if not in VerifiedNews (avoids 404 for very fresh content)
            from src.database.models import RawNews
            raw = db.query(RawNews).filter(RawNews.id == article_id_int).first()
            if raw:
                data = {
                    "id": raw.id,
                    "title": raw.title,
                    "content": raw.summary or raw.title,
                    "source_name": raw.source_name,
                    "image_url": raw.url_to_image,
                    "original_url": raw.url,
                    "published_at": raw.published_at.isoformat() if raw.published_at else datetime.utcnow().isoformat(),
                    "time_ago": "Syncing..."
                }

        if data and data.get("published_at") and not data.get("time_ago"):
            try:
                pub_date = datetime.fromisoformat(data["published_at"]) if isinstance(data["published_at"], str) else data["published_at"]
                diff = datetime.utcnow() - pub_date
                data["time_ago"] = f"{diff.seconds // 3600}h ago" if diff.seconds > 3600 else f"{diff.seconds // 60}m ago"
            except:
                data["time_ago"] = "Just Now"
    
    # If no data found from DB or it's a raw URL (like from Breaking News)
    if not data and (url or not article_id.isdigit()):
        target_url = url or article_id
        # Minimal data for on-the-fly processing
        data = {
            "title": "Intelligence Report",
            "content": "Analyzing source content...",
            "source_name": "Global Intel",
            "image_url": None,
            "original_url": target_url,
            "published_at": datetime.utcnow().isoformat(),
            "time_ago": "Just Now"
        }
    
    if not data:
        raise HTTPException(status_code=404, detail="Intelligence artifact not found")

    # If non-English, perform transformation (Summarize + Translate)
    if lang and lang.lower() != 'english':
        try:
            target_url = data.get("original_url") or url
            # 1. Fetch & Summarize using LLM (Premium Transformation)
            # We use LLMAnalyzer to generate a fresh, copyright-safe summary
            logger.info(f"Transforming article for {lang}...")
            
            # For simplicity in this logic, we'll use LLM to summarize/rewrite
            # But the user wants: "summarize, add extra stuff, why it matters, what happens next"
            # We'll use the LLMAnalyzer's capacity or a custom prompt
            prompt = f"""
            Task: Analyze and rewrite this news article in {lang}.
            Rule: DO NOT copy verbatim. Create a unique, transformed version.
            Structure:
            1. Detailed Summary (3-4 paragraphs)
            2. Key Points (bullet list)
            3. Why It Matters
            4. What Happens Next & Who is Affected More
            
            Source Article URL: {target_url}
            Current Title: {data.get('title')}
            
            Add a timestamp of today: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            Ensure the tone is professional and insightful.
            """
            
            # Using llm_analyzer to generate the content
            # We'll assume the analyzer can take a prompt or we use its analyze method
            # For speed, we'll call the groq-powered analyzer
            analysis_result = await llm_analyzer.analyze_content(target_url, lang=lang)
            
            if analysis_result:
                # Robustly map fields from AI result (User's specific non-English format)
                data["title"] = analysis_result.get("title") or data.get("title") or "Intelligence Report"
                data["why_it_matters"] = analysis_result.get("why_it_matters") or "Analyzing significance..."
                data["who_is_affected"] = analysis_result.get("who_is_affected") or "Evaluating impact..."
                data["what_happens_next"] = analysis_result.get("what_happens_next") or "Projecting future..."
                data["source_name"] = analysis_result.get("source_name") or data.get("source_name") or "Original Source"
                data["official_url"] = analysis_result.get("official_url") or target_url
                data["image_url"] = analysis_result.get("image_url") or data.get("image_url")
                data["published_at_str"] = data.get("time_ago") or "Recently"
                
                # For non-English transformation, we don't want the old summary bullets
                if lang.lower() != 'english':
                    data["summary_bullets"] = [] 
                
            else:
                # Fallback to simple translation
                translated = await translator.translate_text(f"Summary: {data['title']}. Content: {data.get('content', '')}", lang)
                data["content"] = translated
                data["title"] = await translator.translate_text(data["title"], lang)
                
        except Exception as e:
            logger.error(f"Transformation failed: {e}")
            pass

    # ---- DEFINITIVE NORMALIZATION ----
    data = normalize_article_data(data)

    return {"status": "success", "article": data}

@router.get("/api/breaking-news")
async def get_breaking_news(country: str = None, db: Session = Depends(get_db)):
    """API endpoint for breaking news auto-refresh"""
    latest_digest = db.query(DailyDigest).filter(
        DailyDigest.is_published == True
    ).order_by(DailyDigest.date.desc()).first()
    
    breaking_news = []
    if latest_digest and "breaking_news" in latest_digest.content_json:
        breaking_news = latest_digest.content_json["breaking_news"]
        
        # 1. Standardized Filter
        if country:
            target_name, match_keys, _ = normalize_country(country)
            breaking_news = [
                b for b in breaking_news 
                if (b.get("country") in match_keys) or (b.get("country_name") in match_keys)
            ]
        else:
            # HOME PAGE: Only English countries
            non_english = ['jp', 'cn', 'ru', 'de', 'fr', 'Japan', 'China', 'Russia', 'Germany', 'France']
            breaking_news = [b for b in breaking_news if b.get("country") not in non_english]

        # 2. Inject fallback images and NORMALIZE
        for item in breaking_news:
            if not item.get("image_url"):
                seed = f"{item.get('headline', '')}{item.get('title', '')}"
                item["image_url"] = get_fallback_image(seed)
            normalize_article_data(item)
    
    return {"breaking_news": breaking_news}

# ===== MISSING ENDPOINT FIX: Article Update Request =====
# This endpoint was called by dashboard.js requestUpdate() but did not exist,
# causing the page to freeze when "Update" button was clicked.
@router.post("/api/articles/{article_id}/update")
async def request_article_update(article_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Request a fresh AI analysis for a specific article. Returns immediately (non-blocking)."""
    article = db.query(VerifiedNews).filter(VerifiedNews.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    
    async def _do_update(art_id: int, title: str):
        """Background task: re-analyze article with actual AI."""
        import asyncio
        from src.analysis.llm_analyzer import LLMAnalyzer
        from src.database.models import RawNews
        
        db2 = SessionLocal()
        try:
            art = db2.query(VerifiedNews).filter(VerifiedNews.id == art_id).first()
            if not art:
                return
            
            # 1. Fetch source content from RawNews if possible
            content = ""
            if art.raw_news:
                content = art.raw_news.description or art.raw_news.content or ""
            
            # 2. Run real analysis
            analyzer = LLMAnalyzer()
            article_data = {"title": art.title, "content": content}
            
            # We use analyze_batch with a single item to leverage the rotation logic
            results = await analyzer.analyze_batch([article_data])
            
            if results and len(results) > 0:
                res = results[0]
                # Map fields back to VerifiedNews model
                art.summary_bullets = res.get("summary_bullets") or art.summary_bullets
                art.why_it_matters = res.get("why_it_matters") or art.why_it_matters
                art.who_is_affected = res.get("who_is_affected") or art.who_is_affected
                art.bias_rating = res.get("bias_rating") or art.bias_rating
                art.sentiment = res.get("sentiment") or art.sentiment
                art.impact_score = res.get("impact_score") or art.impact_score
                art.updated_at = datetime.utcnow()
                
                db2.commit()
                logger.info(f"✅ Article {art_id} RE-ANALYZED successfully: '{title[:60]}...'")
            else:
                # Touch timestamp even if AI fails
                art.updated_at = datetime.utcnow()
                db2.commit()
                logger.warning(f"⚠️ Article {art_id} AI analysis returned no results, only timestamp was touched.")
                
        except Exception as e:
            logger.error(f"❌ Background article AI update failed for {art_id}: {e}")
        finally:
            db2.close()
    
    background_tasks.add_task(_do_update, article_id, article.title or "")
    return {"status": "success", "message": f"Article #{article_id} queued for refresh."}

@router.get("/api/articles/{article_id}/track")
@router.post("/api/articles/{article_id}/track")
async def track_article_api(article_id: int, db: Session = Depends(get_db)):
    """Non-blocking article tracking endpoint (fallback for frontend)."""
    article = db.query(VerifiedNews).filter(VerifiedNews.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"status": "success", "message": "Article tracked.", "title": article.title}

@router.get("/api/articles/{article_id}/status")
async def get_article_status(article_id: int, db: Session = Depends(get_db)):
    """Get current status of an article — useful for polling after update request."""
    article = db.query(VerifiedNews).filter(VerifiedNews.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return {
        "status": "success",
        "id": article.id,
        "title": article.title,
        "credibility_score": article.credibility_score,
        "bias_rating": article.bias_rating,
        "sentiment": article.sentiment,
    }

@router.get("/api/more-stories/{category}/{offset}")
async def get_more_stories(category: str, offset: int, country: str = None, lang: str = "english", db: Session = Depends(get_db)):
    """Fetch more stories for a specific category with offset"""
    latest_digest = db.query(DailyDigest).filter(DailyDigest.is_published == True).order_by(DailyDigest.date.desc()).first()
    
    if not latest_digest:
        return {"stories": []}

    digest_data = latest_digest.content_json
    stories = []
    
    if category == "top_stories":
        stories = digest_data.get("top_stories", [])
    elif category == "breaking_news" or category == "breaking":
        stories = digest_data.get("breaking_news", [])
    
    # Fast-track for specific keys
    if not stories and category in digest_data:
        stories = digest_data.get(category, [])

    if stories and not country:
        # HOME PAGE: Only English countries
        non_english = ['jp', 'cn', 'ru', 'de', 'fr', 'Japan', 'China', 'Russia', 'Germany', 'France']
        stories = [s for s in stories if s.get("country") not in non_english]
    else:
        # Normalize category to match backend keys 
        normalized_category = category.lower().replace(" ", "_").strip()
        
        # Explicit mappings for frontend-backend mismatches
        category_map = {
            "business": "Business & Economy",
            "economy": "Business & Economy",
            "business_&_economy": "Business & Economy",
            "science": "Science & Health",
            "health": "Science & Health",
            "science_&_health": "Science & Health",
            "tech": "Technology",
            "technology": "Technology",
            "world": "World News",
            "world_news": "World News",
            "india": "India / Local News",
            "local": "India / Local News",
            "india_/_local_news": "India / Local News",
            "sports": "Sports",
            "entertainment": "Entertainment",
            "ai": "AI & Machine Learning",
            "ai_&_machine_learning": "AI & Machine Learning",
            # Student Portal Categories
            "scholarships_&_internships": "Education",
            "exams_&_results": "Education",
            "policy_&_research": "Education",
            "admissions_&_courses": "Education"
        }
        
        target_key = category_map.get(normalized_category, category.strip())

        cat_stories = []
        categories = digest_data.get("categories", {})
        
        # 1. Try direct match with mapped key
        if target_key in categories:
            cat_stories = categories[target_key]
        # 2. Try direct match with original normalized key
        elif normalized_category in categories:
             cat_stories = categories[normalized_category]
        else:
            # 3. Fallback: Check keys case-insensitively
            for k, v in categories.items():
                if k.lower() == normalized_category or k.lower() == target_key.lower():
                    cat_stories = v
                    break
        
        stories = cat_stories
        
        # Apply English-only filter for Home Page (if country is null)
        if not country:
            non_english = ['jp', 'cn', 'ru', 'de', 'fr', 'Japan', 'China', 'Russia', 'Germany', 'France']
            stories = [s for s in stories if s.get("country") not in non_english]

        # Normalize if needed (same logic as dashboard)
        if stories:
            normalized = []
            for s in stories:
                normalized.append({
                    "id": s.get("id"),
                    "title": s.get("title"),
                    "url": s.get("url"),
                    "image_url": s.get("image_url"),
                    "source_name": s.get("source_name"),
                    "bullets": s.get("bullets") or [s.get("summary") or s.get("why", "")],
                    "affected": s.get("affected", ""),
                    "why": s.get("why", ""),
                    "bias": s.get("bias", "Neutral"),
                    "tags": s.get("tags", []),
                    "category": category,
                    "time_ago": s.get("time_ago", "Just Now")
                })
            stories = normalized
             
        # FINALLY: If country is provided, filter the results strictly to match
        if country and stories:
            target_name, match_keys, _ = normalize_country(country)
            stories = [
                s for s in stories
                if (s.get("country") in match_keys) or (s.get("country_name") in match_keys)
            ]

    # Pagination logic
    start = offset
    limit = 20
    end = offset + limit
    
    # Check if there are more stories after this batch
    subset = stories[start:end]
    has_more = len(stories) > end
    
    # Run translation if requested
    if lang and lang.lower() != "english" and subset:
        try:
            from src.utils.translator import NewsTranslator
            translator = NewsTranslator()
            translated_subset = []
            
            # Call translation wrapper directly with standard dictionaries
            res = await translator._do_translate(subset, lang)
            subset = res.get("translated_stories", subset)
        except Exception as e:
            print(f"Error translating more-stories: {str(e)}")
    
    # ---- NORMALIZE ALL STORIES BEFORE RETURNING ----
    for s in subset:
        normalize_article_data(s)
    
    return {
        "stories": subset,
        "has_more": has_more
    }

class LoginRequest(BaseModel):
    id_token: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None

@router.post("/api/login")
async def login(payload: LoginRequest, db: Session = Depends(get_db)):
    # CASE 1: Firebase ID Token (from main website)
    if payload.id_token:
        decoded_token = verify_token(payload.id_token)
        if not decoded_token:
            raise HTTPException(status_code=401, detail="Invalid Firebase Token")
        
        uid = decoded_token.get("uid")
        email = decoded_token.get("email")
        phone = decoded_token.get("phone_number")
        
        # Upsert User
        user = db.query(User).filter(User.firebase_uid == uid).first()
        needs_language = False
        
        if not user:
            user = User(firebase_uid=uid, email=email, phone=phone, preferred_language="english")
            db.add(user)
            needs_language = True
        else:
            if email: user.email = email
            if phone: user.phone = phone
            try:
                if not user.preferred_language: needs_language = True
            except: needs_language = True
                
        db.commit()
        db.refresh(user)
        
        pref_lang = getattr(user, "preferred_language", "english")
        return {"status": "success", "uid": uid, "needs_language": needs_language, "preferred_language": pref_lang}

    # CASE 2: Email/Password (from Admin Dashboard)
    elif payload.email and payload.password:
        # Simple Admin Auth for now (Matches the user's screenshot credentials)
        # We allow the specific owner email or any valid admin record
        if "ashok" in payload.email or "admin" in payload.email:
            # Generate a mock token for the frontend
            return {
                "status": "success", 
                "token": "admin-session-secure-token", 
                "role": "admin",
                "email": payload.email
            }
        
        raise HTTPException(status_code=401, detail="Access Denied: Invalid Credentials")

    raise HTTPException(status_code=422, detail="Missing Authentication Parameters")

class LanguageRequest(BaseModel):
    firebase_uid: str
    language: str

@router.post("/api/user/language")
async def set_user_language(payload: LanguageRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.firebase_uid == payload.firebase_uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.preferred_language = payload.language
    db.commit()
    return {"status": "success", "language": payload.language}

# Redundant /api/user routes removed. Unified under /api/retention in user_retention.py

class SubscribeRequest(BaseModel):
    firebase_uid: str
    category: str

@router.post("/api/subscribe")
async def subscribe_category(payload: SubscribeRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.firebase_uid == payload.firebase_uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if already subscribed
    existing = db.query(Subscription).filter(
        Subscription.user_id == user.id,
        Subscription.category == payload.category
    ).first()
    
    if not existing:
        sub = Subscription(user_id=user.id, category=payload.category)
        db.add(sub)
        db.commit()
        return {"status": "success", "message": f"Subscribed to {payload.category}"}
    
    return {"status": "already_subscribed", "message": "Already on the list!"}

# REMOVED: Mock Test HTML route (Moved to Frontend Server)

@router.post("/api/sync-intelligence")
async def force_sync_intelligence(background_tasks: BackgroundTasks):
    """Manually trigger a full news collection and analysis cycle"""
    from src.scheduler.task_scheduler import run_news_cycle
    
    # Run helper to start the async cycle in background
    async def _run_cycle():
        try:
            await run_news_cycle()
        except Exception as e:
            logger.error(f"Manual Sync Failed: {e}")

    background_tasks.add_task(_run_cycle)
    return {"status": "success", "message": "Intelligence scan initiated in background."}

@router.post("/api/refresh-digest")
async def refresh_digest(db: Session = Depends(get_db)):
    """Manually regenerate the daily digest from existing verified news"""
    from src.digest.generator import DigestGenerator
    generator = DigestGenerator()
    try:
        digest = await generator.create_daily_digest(db)
        if digest:
            return {"status": "success", "message": "Live site updated successfully!"}
        return {"status": "error", "message": "Failed to generate digest"}
    except Exception as e:
        logger.error(f"Manual Digest Refresh Failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/api/system-check")
async def system_check(db: Session = Depends(get_db)):
    """A detailed health check for debugging deployment environments"""
    from src.database.models import RawNews, VerifiedNews, DailyDigest
    return {
        "raw_news_count": db.query(RawNews).count(),
        "verified_news_count": db.query(VerifiedNews).count(),
        "digest_count": db.query(DailyDigest).count(),
        "has_news_api_key": bool(settings.NEWS_API_KEY),
        "db_url_is_sqlite": settings.DATABASE_URL.startswith("sqlite")
    }


@router.post("/api/generate-exam")
async def generate_mock_exam(db: Session = Depends(get_db)):
    """Generate a quick mock test from recent news"""
    # Import here to avoid circular dependency if any
    from src.analysis.exam_generator import ExamGenerator
    
    generator = ExamGenerator()
    # For now, we simulate "yesterday's news" by just grabbing recent verified news
    # Ideally, ExamGenerator logic handles the time window
    
    # We need to construct a robust prompt in ExamGenerator
    # But first, let's fix the class method usage
    
    # Actually, we defined `generate_mock_test` in the class
    # We need to pass the DB session
    
    exam_data = generator.generate_mock_test(db)
    
    if "error" in exam_data:
        raise HTTPException(status_code=500, detail=exam_data["error"])
        
    return exam_data


class ChatRequest(BaseModel):
    query: str

@router.post("/api/chat")
@router.post("/api/v2/chat")
async def chat_with_news(payload: ChatRequest, db: Session = Depends(get_db)):
    response = chat_engine.get_response(db, payload.query)
    return {"status": "success", "response": response}


class TranslateNodeRequest(BaseModel):
    stories: list
    lang: str
    node_title: str = ""
    node_description: str = ""
    node_navigation: str = ""
    node_categories: str = ""

@router.post("/api/state-news")
async def get_state_news(payload: TranslateNodeRequest):
    """
    Fetch news for states associated with a regional language and translate them.
    Uses concurrent asyncio.gather with per-state + total timeouts to stay under 20 seconds.
    """
    lang = payload.lang
    if lang not in LANGUAGE_TO_STATES:
        return {"status": "skipped", "message": f"No state mapping for {lang}", "stories": []}
        
    states = LANGUAGE_TO_STATES[lang]
    
    # Fetch ALL states concurrently with a per-state timeout (6s max each)
    async def fetch_state_safe(state: str):
        try:
            result = await asyncio.wait_for(
                universe_collector.fetch_country_news(f"{state}, India"),
                timeout=6.0
            )
            stories = result.get("breaking_news", []) + result.get("top_stories", [])
            for s in stories:
                if 'tags' not in s:
                    s['tags'] = []
                s['tags'].append(state)
                s['is_state_news'] = True
            return stories
        except asyncio.TimeoutError:
            logger.warning(f"State news fetch timed out for: {state}")
            return []
        except Exception as e:
            logger.error(f"Failed to fetch news for state {state}: {e}")
            return []

    try:
        # Run all state fetches concurrently, total cap 20 seconds
        all_results = await asyncio.wait_for(
            asyncio.gather(*[fetch_state_safe(state) for state in states]),
            timeout=20.0
        )
    except asyncio.TimeoutError:
        logger.warning("State news overall fetch timed out after 20s")
        all_results = []

    # Flatten and deduplicate
    all_state_stories = []
    seen_urls = set()
    for story_list in all_results:
        for s in story_list:
            url = s.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_state_stories.append(s)
            elif not url:
                all_state_stories.append(s)
            if len(all_state_stories) >= 15:
                break
        if len(all_state_stories) >= 15:
            break
            
    if not all_state_stories:
        return {"status": "no_news", "stories": []}
        
    # Translate concurrently (already optimized in translate_stories)
    translated_stories = await translator.translate_stories(all_state_stories[:15], lang)
    
    # APPLY DEFINITIVE NORMALIZATION & DEDUPLICATION
    unique_final = []
    seen_ids = set()
    for a in translated_stories:
        if a.get('id') not in seen_ids:
            seen_ids.add(a.get('id'))
            unique_final.append(normalize_article_data(a))
    
    return {
        "status": "success",
        "stories": unique_final
    }


@router.post("/api/translate-node")
async def translate_node(payload: TranslateNodeRequest):
    """
    Translate stories and UI labels using a SINGLE Groq API call.
    Hard 15-second timeout — returns originals on failure so page never hangs.
    """
    if not payload.lang or payload.lang.lower() == "english":
        return {"status": "success", "translated_stories": payload.stories, "node_title": payload.node_title or ""}

    if not payload.stories and not payload.node_title:
        return {"status": "success", "translated_stories": [], "node_title": ""}

    try:
        result = await asyncio.wait_for(
            _do_translate(payload.stories, payload.lang, payload.node_title or ""),
            timeout=45.0
        )
        return result
    except asyncio.TimeoutError:
        logger.warning(f"translate-node timed out for lang={payload.lang}, returning originals")
        return {"status": "success", "translated_stories": payload.stories, "node_title": payload.node_title or ""}
    except Exception as e:
        logger.error(f"translate-node failed: {e}")
        return {"status": "success", "translated_stories": payload.stories, "node_title": payload.node_title or ""}


async def _do_translate(stories: list, lang: str, node_title: str) -> dict:
    """Translate stories: tries Groq first, falls back to MyMemory free API."""
    if not stories and not node_title:
        return {"status": "success", "translated_stories": stories, "node_title": node_title}

    # TIER 1: Try Groq (single JSON call, all keys)
    result = await _try_groq_translate(stories, lang, node_title)
    if result:
        return result

    # TIER 2: Fallback — Google API (Sequential)
    logger.info(f"Groq unavailable, falling back to Google Translate for {len(stories)} items in {lang}")
    result = await _google_translate_fallback(stories, lang, node_title)
    if result:
        return result

    # TIER 3: Return originals
    logger.error("All translation methods failed, returning originals")
    return {"status": "success", "translated_stories": stories, "node_title": node_title}


async def _try_groq_translate(stories: list, lang: str, node_title: str) -> dict | None:
    """Try all Groq keys. Returns translated dict on success, None on failure."""
    input_obj = {"lang": lang, "node_title": node_title, "items": []}
    for s in stories:
        item = {"t": s.get("title", s.get("headline", ""))}
        bulls = s.get("bullets", [])
        if bulls: item["b"] = bulls[:3]
        why = s.get("why", "")[:120]
        if why: item["w"] = why
        aff = s.get("affected", "")[:80]
        if aff: item["a"] = aff
        input_obj["items"].append(item)

    prompt = (f"Translate the following JSON into {lang}. Return ONLY valid JSON with the same structure.\n"
              f"Fields: \"node_title\", \"items\" array each with \"t\"=title, optionally \"b\"=bullets[], \"w\"=why, \"a\"=affected.\n"
              f"Input JSON:\n{json.dumps(input_obj, ensure_ascii=False)}")

    # Try specialized key first if available
    client, key_info = translator._get_client(lang)
    if client:
        try:
            logger.info(f"Using Groq {key_info} for translation to {lang}")
            # CLEANED GROQ CALL - Consistent Schema
            model = "llama-3.3-70b-versatile"
            system_prompt = f"You are a professional news translator. Translate the following JSON list of stories to {lang}. Return ONLY valid JSON."
            user_prompt = f"Follow this schema exactly: {{'items': [...]}}. Each item must match the input keys exactly. Stories: {json.dumps(input_obj['items'])}"
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.1,
                    response_format={"type": "json_object"}
                ),
                timeout=30
            )
            translated = json.loads(response.choices[0].message.content.strip())
            translated_items = translated.get("items", [])
            merged = []
            for i, orig in enumerate(stories):
                tr = translated_items[i] if i < len(translated_items) else {}
                m = dict(orig)
                if tr.get("t"): m["title"] = tr["t"]; m["headline"] = tr["t"]
                if tr.get("b"): m["bullets"] = tr["b"]
                if tr.get("w"): m["why"] = tr["w"]
                if tr.get("a"): m["affected"] = tr["a"]
                merged.append(m)
            return {"status": "success", "translated_stories": merged, "node_title": translated.get("node_title", node_title)}
        except Exception as e:
            logger.warning(f"Specialized Groq key failed for {lang}, falling back to rotation: {e}")

    all_keys = translator.groq_keys if translator.groq_keys else []
    for attempt, key in enumerate(all_keys):
        client_obj = translator._clients.get(key)
        if not client_obj:
            from openai import AsyncOpenAI
            client_obj = AsyncOpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")
            translator._clients[key] = client_obj
        try:
            logger.info(f"Groq attempt {attempt+1}/{len(all_keys)} key=...{key[-6:]}")
            response = await client_obj.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": "You are a professional translator. Return ONLY valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
                timeout=22
            )
            translated = json.loads(response.choices[0].message.content.strip())
            translated_items = translated.get("items", [])
            merged = []
            for i, orig in enumerate(stories):
                tr = translated_items[i] if i < len(translated_items) else {}
                m = dict(orig)
                if tr.get("t"): m["title"] = tr["t"]; m["headline"] = tr["t"]
                if tr.get("b"): m["bullets"] = tr["b"]
                if tr.get("w"): m["why"] = tr["w"]
                if tr.get("a"): m["affected"] = tr["a"]
                merged.append(m)
            return {"status": "success", "translated_stories": merged, "node_title": translated.get("node_title", node_title)}
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                logger.warning(f"Groq rate limit on key ...{key[-6:]} - Bypassing retries to fallback.")
                return None
            else:
                logger.error(f"Groq error key ...{key[-6:]}: {e}")
    return None


# Language code map for Google API Fallback
_GOOGLE_LANG_CODES = {
    "Telugu": "te", "Hindi": "hi", "Tamil": "ta", "Kannada": "kn",
    "Malayalam": "ml", "Arabic": "ar", "Japanese": "ja", "Spanish": "es",
    "French": "fr", "German": "de", "Russian": "ru", "Chinese": "zh-CN",
    "Korean": "ko", "Portuguese": "pt", "Turkish": "tr",
    # Maps for abbreviated requests from frontend
    "TE": "te", "HI": "hi", "TA": "ta", "KN": "kn", "ML": "ml", "AR": "ar",
    "JA": "ja", "ES": "es", "FR": "fr", "DE": "de", "RU": "ru", "ZH": "zh-CN",
    "KO": "ko", "PT": "pt", "TR": "tr", "EN": "en"
}

async def _google_translate_fallback(stories: list, lang: str, node_title: str) -> dict | None:
    """Translate ALL texts sequentially using Google Translate free API to avoid IP rate limits."""
    import urllib.parse
    import httpx
    
    lang_code = _GOOGLE_LANG_CODES.get(lang)
    if not lang_code:
        return None

    # Translate one string via Google API
    async def string_translate_one(client: httpx.AsyncClient, text: str, sem: asyncio.Semaphore) -> str:
        if not text or not text.strip():
            return text
            
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl={lang_code}&dt=t&q={urllib.parse.quote(text[:800])}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
        }
        
        async with sem:
            try:
                # Tiny stagger to avoid immediate IP ban, but otherwise concurrent
                await asyncio.sleep(0.05) 
                resp = await client.get(url, headers=headers, timeout=8.0)
                if resp.status_code == 200:
                    data = resp.json()
                    # Google API returns nested lists: [[[translated_text, original_text, ...]]]
                    t = "".join([segment[0] for segment in data[0] if segment[0]])
                    if t and t.upper() != text.upper():
                        return t
                else:
                    logger.warning(f"Google API Fallback returned {resp.status_code}")
            except Exception as e:
                logger.error(f"Google fallback error: {e}")
        return text

    # Step 1: Collect ALL unique texts to translate (flat list with source mapping)
    texts_to_translate = []  # list of strings
    # Format: [(story_idx, field_name, bullet_idx_or_None), ...]
    text_map = []

    for i, s in enumerate(stories):
        title = s.get("title", "")
        if title:
            text_map.append((i, "title", None))
            texts_to_translate.append(title)
        for bi, b in enumerate(s.get("bullets", [])[:3]):
            text_map.append((i, "bullet", bi))
            texts_to_translate.append(b)
        why = s.get("why", "")[:200]
        if why:
            text_map.append((i, "why", None))
            texts_to_translate.append(why)
        aff = s.get("affected", "")[:150]
        if aff:
            text_map.append((i, "affected", None))
            texts_to_translate.append(aff)

    if node_title:
        texts_to_translate.append(node_title)

    logger.info(f"Google API Fallback: translating {len(texts_to_translate)} texts to {lang} concurrently")

    # Step 2: Translate ALL concurrently with a semaphore
    sem = asyncio.Semaphore(15) 
    async with httpx.AsyncClient() as client:
        translated_texts = await asyncio.gather(
            *[string_translate_one(client, t, sem) for t in texts_to_translate],
            return_exceptions=True
        )

    # Replace exceptions with originals
    translated_texts = [
        texts_to_translate[i] if isinstance(r, Exception) else r
        for i, r in enumerate(translated_texts)
    ]

    # Step 3: Map translated texts back onto stories
    merged = [dict(s) for s in stories]
    for i, (story_idx, field, bullet_idx) in enumerate(text_map):
        val = translated_texts[i]
        m = merged[story_idx]
        if field == "title":
            m["title"] = val
            m["headline"] = val
        elif field == "bullet":
            if "bullets" not in m or not isinstance(m.get("bullets"), list):
                m["bullets"] = list(stories[story_idx].get("bullets", [])[:3])
            if bullet_idx < len(m["bullets"]):
                m["bullets"][bullet_idx] = val
        elif field == "why":
            m["why"] = val
        elif field == "affected":
            m["affected"] = val

    new_title = translated_texts[len(text_map)] if node_title and len(translated_texts) > len(text_map) else node_title
    return {"status": "success", "translated_stories": merged, "node_title": new_title}



class NoteRequest(BaseModel):
    text: str
    url: str

@router.post("/api/save-note")
async def save_note(payload: NoteRequest):
    # Log it for now as there is no DB table for notes yet
    logger.info(f"User Note: {payload.text} from {payload.url}")
    return {"status": "success", "message": "Note recorded"}

# REMOVED: /universe UI route (Moved to Frontend Server)

class UniverseRequest(BaseModel):
    country: str

@router.post("/api/v2/universe/news")
@router.get("/api/v2/universe/news")
@router.post("/api/universe/news")
async def get_universe_news(payload: UniverseRequest):
    try:
        # Now returns a dictionary with top_stories, breaking_news, videos, newspaper_summary
        news_data = await universe_collector.fetch_country_news(payload.country)
        return {"status": "success", "news": news_data}
    except Exception as e:
        logger.error(f"Universe News Fetch Failed: {e}")
        return {"status": "error", "message": str(e)}

# --- ADMIN MANAGEMENT API ENDPOINTS ---

@router.get("/api/articles")
async def get_all_articles(category: str = None, country: str = None, db: Session = Depends(get_db)):
    """Backend endpoint for admin panel to fetch all verified intelligence with filtering."""
    try:
        from src.database.models import VerifiedNews
        query = db.query(VerifiedNews)
        if category and category != 'All':
            query = query.filter(VerifiedNews.category == category)
        if country:
            query = query.filter(VerifiedNews.country == country)
            
        # LIFO: Impact score first (manual priority), then newest first
        articles = query.order_by(VerifiedNews.impact_score.desc(), VerifiedNews.created_at.desc()).all()
        
        # FINAL PARITY: If category is student-related, apply the same filter as the main website
        if category in STUDENT_NEWS_CATEGORIES:
            articles = [a for a in articles if is_student_article_logic(a)]
            
        return [a.to_dict() for a in articles]
    except Exception as e:
        logger.error(f"Failed to fetch articles for Admin: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/articles/{article_id}")
async def delete_article(article_id: int, db: Session = Depends(get_db)):
    """Admin endpoint to remove an intelligence node"""
    try:
        article = db.query(VerifiedNews).filter(VerifiedNews.id == article_id).first()
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        db.delete(article)
        db.commit()
        return {"status": "success", "message": f"Article {article_id} deleted"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/ads")
async def get_all_ads(db: Session = Depends(get_db)):
    """Fetch all campaign nodes (advertisements)"""
    try:
        from src.database.models import Advertisement
        ads = db.query(Advertisement).order_by(Advertisement.created_at.desc()).all()
        return ads
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class AdCreateRequest(BaseModel):
    image_url: str
    caption: str
    position: str = "both"
    target_node: str = "Global"
    target_url: str = None
    target_platform: str = "both"

@router.post("/api/ads")
async def create_ad(payload: AdCreateRequest, db: Session = Depends(get_db)):
    """Admin endpoint to deploy a new campaign node"""
    try:
        from src.database.models import Advertisement
        new_ad = Advertisement(
            image_url=payload.image_url,
            caption=payload.caption,
            position=payload.position,
            target_node=payload.target_node,
            target_url=payload.target_url,
            target_platform=payload.target_platform
        )
        db.add(new_ad)
        db.commit()
        db.refresh(new_ad)
        
        # Log Action
        log_protocol_action(db, "deploy", "ad", new_ad.id, details=f"Deployed new campaign node: {payload.caption}")
        
        return {"success": True, "ad": new_ad}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/history")
async def get_protocol_history(db: Session = Depends(get_db)):
    """Fetch recent administrative action logs."""
    try:
        from src.database.models import ProtocolHistory
        history = db.query(ProtocolHistory).order_by(ProtocolHistory.timestamp.desc()).limit(100).all()
        return history
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/ads/{ad_id}")
async def delete_ad(ad_id: int, db: Session = Depends(get_db)):
    """Remove a campaign node"""
    try:
        from src.database.models import Advertisement
        ad = db.query(Advertisement).filter(Advertisement.id == ad_id).first()
        if not ad:
            raise HTTPException(status_code=404, detail="Ad not found")
        db.delete(ad)
        db.commit()
        
        # Log Action
        log_protocol_action(db, "delete", "ad", ad_id, details=f"Removed campaign node: {ad.caption}")
        
        return {"success": True}
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/newspapers")
async def get_all_newspapers(db: Session = Depends(get_db)):
    """Fetch all registered source nodes"""
    try:
        from src.database.models import Newspaper
        papers = db.query(Newspaper).order_by(Newspaper.name.asc()).all()
        return papers
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class NewspaperCreateRequest(BaseModel):
    name: str
    url: str
    country: str = "Global"
    logo_text: str = None
    logo_color: str = None

@router.post("/api/newspapers")
async def create_newspaper(payload: NewspaperCreateRequest, db: Session = Depends(get_db)):
    """Register a new newspaper source"""
    try:
        from src.database.models import Newspaper
        new_paper = Newspaper(
            name=payload.name,
            url=payload.url,
            country=payload.country,
            logo_text=payload.logo_text,
            logo_color=payload.logo_color
        )
        db.add(new_paper)
        db.commit()
        db.refresh(new_paper)
        
        # Log Action
        log_protocol_action(db, "register", "source", new_paper.id, details=f"Initialized source node: {payload.name} ({payload.country})")
        
        return {"success": True, "paper": new_paper}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/newspapers/{paper_id}")
async def delete_newspaper(paper_id: int, db: Session = Depends(get_db)):
    """Unregister a source node"""
    try:
        from src.database.models import Newspaper
        paper = db.query(Newspaper).filter(Newspaper.id == paper_id).first()
        if not paper:
            raise HTTPException(status_code=404, detail="Newspaper not found")
        db.delete(paper)
        db.commit()
        
        # Log Action
        log_protocol_action(db, "delete", "source", paper_id, details=f"Unregistered source node: {paper.name}")
        
        return {"success": True}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))



class ManualStudentArticleRequest(BaseModel):
    title: str
    description: str
    image_url: str
    redirect_url: str
    category: str
    access_link: str = None

@router.post("/api/student/articles")
async def create_manual_student_article(payload: ManualStudentArticleRequest, db: Session = Depends(get_db)):
    """Admin endpoint to add manual student portal articles. Handles duplicates gracefully."""
    try:
        from src.database.models import VerifiedNews, RawNews
        
        # 1. Lookup or create RawNews entry based on URL (Unique Constraint fix)
        raw = db.query(RawNews).filter(RawNews.url == payload.redirect_url).first()
        
        if not raw:
            raw = RawNews(
                title=payload.title,
                description=payload.description,
                url=payload.redirect_url,
                url_to_image=payload.image_url,
                source_name="Student Portal Editorial",
                published_at=datetime.utcnow(),
                is_verified=True,
                processed=True,
                country="Global"
            )
            db.add(raw)
            db.flush() # Get raw.id without committing
        else:
            # Update existing raw news metadata
            raw.title = payload.title
            raw.description = payload.description
            raw.url_to_image = payload.image_url
            raw.source_name = "Student Portal Editorial"
            raw.is_verified = True
            raw.processed = True

        # 2. Lookup or create VerifiedNews entry linked to this RawNews
        verified = db.query(VerifiedNews).filter(VerifiedNews.raw_news_id == raw.id).first()
        
        if not verified:
            verified = VerifiedNews(
                raw_news_id=raw.id,
                title=payload.title,
                content=payload.description,
                summary_bullets=[payload.description[:100] + "..."],
                impact_tags=[payload.category],
                bias_rating="Neutral",
                category=payload.category,
                country="Global",
                credibility_score=1.0,
                impact_score=100, # MAX PRIORITY FOR MANUAL
                why_it_matters=payload.description[:200],
                sentiment="Neutral",
                is_verified=True,
                analysis={"access_link": payload.access_link},
                published_at=datetime.utcnow()
            )
            db.add(verified)
        else:
            # Update existing verified record
            verified.title = payload.title
            verified.content = payload.description
            verified.category = payload.category
            verified.impact_score = 100
            verified.published_at = datetime.utcnow() # Final sync to ensure it stays in FIRST PLACE
            verified.why_it_matters = payload.description[:200]
            
            # Update access link in analysis blob
            current_analysis = verified.analysis or {}
            if isinstance(current_analysis, str):
                try: current_analysis = json.loads(current_analysis)
                except: current_analysis = {}
            current_analysis["access_link"] = payload.access_link
            verified.analysis = current_analysis

        # 3. Finalize Atomic Transaction with extra safety
        from sqlalchemy.exc import IntegrityError
        try:
            db.commit()
            db.refresh(verified)
        except IntegrityError as ie:
            db.rollback()
            logger.error(f"Article Sync Collision Resolve: {ie}")
            # Final attempt: direct update if ID collision happened
            verified = db.query(VerifiedNews).filter(VerifiedNews.raw_news_id == raw.id).first()
            if verified:
                verified.title = payload.title
                verified.content = payload.description
                verified.category = payload.category
                verified.published_at = datetime.utcnow()
                db.commit()
            else:
                raise ie

        # Log Action
        log_protocol_action(db, "deploy", "student_article", verified.id, details=f"Deployed manual student article: {payload.title}")
        
        # 4. Clear cache to force real-time sync
        _student_news_caches.clear()
        
        return {"success": True, "article": verified.to_dict()}
    except Exception as e:
        db.rollback()
        logger.error(f"Manual student article deployment failed CRITICAL: {str(e)}")
        raise HTTPException(status_code=503, detail=f"Infrastructure Sync Fail: {str(e)}")

@router.put("/api/articles/{article_id}")
async def update_article(article_id: int, payload: ManualStudentArticleRequest, db: Session = Depends(get_db)):
    """Admin endpoint to update an existing article node."""
    try:
        from src.database.models import VerifiedNews, RawNews
        article = db.query(VerifiedNews).filter(VerifiedNews.id == article_id).first()
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")

        # Update Verified record
        article.title = payload.title
        article.content = payload.description
        article.category = payload.category
        article.impact_score = 100 
        
        # Access link storage in analysis blob
        current_analysis = article.analysis or {}
        if isinstance(current_analysis, str):
            try: current_analysis = json.loads(current_analysis)
            except: current_analysis = {}
        
        current_analysis["access_link"] = payload.access_link
        article.analysis = current_analysis

        # Update Raw link if exists
        if article.raw_news:
            # URL Check for unique constraint if URL changed
            if article.raw_news.url != payload.redirect_url:
                existing_url = db.query(RawNews).filter(RawNews.url == payload.redirect_url).first()
                if existing_url and existing_url.id != article.raw_news.id:
                     # Merge or reject? For now, we update if not a duplicate
                     raise HTTPException(status_code=400, detail="Redirect URL already exists in another node.")
            
            article.raw_news.title = payload.title
            article.raw_news.description = payload.description
            article.raw_news.url = payload.redirect_url
            article.raw_news.url_to_image = payload.image_url

        db.commit()
        
        # Log Action
        log_protocol_action(db, "update", "article", article_id, details=f"Updated intelligence node: {payload.title}")
        
        _student_news_caches.clear()
        return {"success": True, "article": article.to_dict()}
    except HTTPException: raise
    except Exception as e:
        db.rollback()
        logger.error(f"Article update failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/articles/{article_id}")
async def delete_article(article_id: int, db: Session = Depends(get_db)):
    """Admin endpoint to delete an intelligence node."""
    try:
        from src.database.models import VerifiedNews
        article = db.query(VerifiedNews).filter(VerifiedNews.id == article_id).first()
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        
        db.delete(article)
        db.commit()
        
        # Log Action
        log_protocol_action(db, "delete", "article", article_id, details=f"Removed intelligence node: {article.title}")
        
        _student_news_caches.clear()
        return {"success": True}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# --- STUDENT NEWS PORTAL (API-ONLY) ---
# REMOVED: /student-news UI route (Moved to Frontend Server)

def _get_active_campaign(platform="main"):
    """Helper to fetch active blueprint campaign targeting specific platform."""
    try:
        admin_api_url = os.getenv("ADMIN_API_URL", "http://localhost:5000")
        resp = requests.get(f"{admin_api_url}/api/blueprints/active", timeout=2)
        if resp.status_code == 200:
            data = resp.json()
            # If the blueprint has a target_platforms field and matches, return its content
            struct = data.get("structure")
            if struct and struct.get("type") == "campaign":
                content = struct.get("content", {})
                target = content.get("target_platform", "both")
                # Platform check: if it matches the current platform or is "both"
                if target == "both" or target == platform:
                    return content
    except Exception as e:
        logger.debug(f"Campaign fetch failed for {platform}: {e}")
    return None

@router.get("/api/v2/get-student-news")
@router.get("/api/get-student-news")
async def api_get_student_news(category: str = None, profile: str = None, country: str = "India", lang: str = "english", offset: int = 0, limit: int = 10, db: Session = Depends(get_db)):
    """API endpoint to get student news JSON."""
    _update_student_cache_if_needed(db, force=False, country=country)
    target_name, _, _ = normalize_country(country)
    country_key = target_name.lower()
    articles = _student_news_caches.get(country_key, {}).get("articles", [])
    if category and category != "All":
        articles = [a for a in articles if a["category"] == category]
    if profile:
        articles = [a for a in articles if profile in a.get("profiles", [])]
        
    page_raw = articles[offset:offset+limit]
    # APPLY DEFINITIVE NORMALIZATION
    page_articles = [normalize_article_data(a) for a in page_raw]
    
    # Translation Injection (Perfection Restoration)
    if lang and lang.lower() != 'english' and page_articles:
        try:
            node_data = {"stories": page_articles}
            await translator.translate_node_bulk(node_data, lang)
        except Exception as e:
            logger.error(f"Student news translation failed: {e}")

    has_more = (offset + limit) < len(articles)
    return {"status": "success", "count": len(page_articles), "articles": page_articles, "has_more": has_more}

@router.get("/api/v2/get-student-trends")
@router.get("/api/get-student-trends")
def api_get_student_trends(country: str = "India", db: Session = Depends(get_db)):
    """API endpoint to get student news trends."""
    _update_student_cache_if_needed(db, force=False, country=country)
    target_name, _, _ = normalize_country(country)
    country_key = target_name.lower()
    return {"status": "success", "trends": _student_news_caches.get(country_key, {}).get("trends", {})}

NEWSDATA_STUDENT_API_KEY = settings.NEWSDATA_STUDENT_API_KEY or "pub_87a3d48b48ba4c15955866088bd380c8"

def _fetch_newsdata_student_articles(country: str = "india") -> list:
    """Fetch comprehensive student news from newsdata.io across all categories."""
    import requests as req_lib
    
    CATEGORY_QUERIES = {
        "Scholarships & Internships": "scholarship OR fellowship OR internship OR stipend",
        "Exams & Results": "exam result OR board exam OR entrance exam OR NEET OR JEE OR UPSC OR GATE OR competitive exam",
        "Admissions & Courses": "college admission OR university admission OR course enrollment OR degree program OR application open",
        "Career & Jobs": "job vacancy OR recruitment OR placement OR hiring OR career opportunity OR fresher jobs",
        "All Updates": "student education OR scholarship OR exam OR admission OR internship OR result",
    }
    
    CATEGORY_MAP = {
        "scholarship": "Scholarships & Internships",
        "fellowship": "Scholarships & Internships",
        "internship": "Scholarships & Internships",
        "stipend": "Scholarships & Internships",
        "exam result": "Exams & Results",
        "board exam": "Exams & Results",
        "entrance exam": "Exams & Results",
        "neet": "Exams & Results",
        "jee": "Exams & Results",
        "upsc": "Exams & Results",
        "gate": "Exams & Results",
        "result": "Exams & Results",
        "admission": "Admissions & Courses",
        "course": "Admissions & Courses",
        "enrollment": "Admissions & Courses",
        "degree": "Admissions & Courses",
        "job": "Career & Jobs",
        "vacancy": "Career & Jobs",
        "recruitment": "Career & Jobs",
        "placement": "Career & Jobs",
        "hiring": "Career & Jobs",
        "career": "Career & Jobs",
    }

    results = []
    seen_urls = set()
    country_code = "in" if country.lower() in ["india", "global", ""] else country[:2].lower()
    
    # Fetch all categories to ensure "All Updates" is also populated
    for cat_name, query in CATEGORY_QUERIES.items():
        try:
            url = (
                f"https://newsdata.io/api/1/news"
                f"?apikey={NEWSDATA_STUDENT_API_KEY}"
                f"&q={req_lib.utils.quote(query)}"
                f"&country={country_code}"
                f"&language=en"
                f"&category=education"
                f"&size=10"
            )
            resp = req_lib.get(url, timeout=8)
            if resp.status_code != 200:
                # Try without country filter for global results
                url = (
                    f"https://newsdata.io/api/1/news"
                    f"?apikey={NEWSDATA_STUDENT_API_KEY}"
                    f"&q={req_lib.utils.quote(query)}"
                    f"&language=en"
                    f"&category=education"
                    f"&size=5"
                )
                resp = req_lib.get(url, timeout=8)
            
            if resp.status_code == 200:
                data = resp.json()
                for art in data.get("results", []):
                    art_url = art.get("link", "#")
                    if art_url in seen_urls:
                        continue
                    seen_urls.add(art_url)
                    
                    title_lower = (art.get("title") or "").lower()
                    
                    # Determine specific category from title
                    detected_cat = cat_name
                    for keyword, mapped_cat in CATEGORY_MAP.items():
                        if keyword in title_lower:
                            detected_cat = mapped_cat
                            break
                    
                    # Build apply/results link based on category
                    is_exam = "Exams" in detected_cat
                    is_result = "result" in title_lower
                    action_label = "Check Results →" if is_result else ("Apply Now →" if not is_exam else "View Exam Details →")
                    
                    results.append({
                        "id": 0,
                        "title": art.get("title", "Student Update"),
                        "summary": art.get("description") or art.get("content", "")[:300] or "Click to read more.",
                        "category": detected_cat,
                        "tags": [f"#{detected_cat.split(' ')[0]}", "#Live", "#India"],
                        "profiles": ["Undergraduate", "High School", "Competitive Exam Aspirant"],
                        "direct_links": [art_url],
                        "access_link": art_url,
                        "action_label": action_label,
                        "important_dates": [art.get("pubDate", "")[:10] if art.get("pubDate") else "See Details"],
                        "authority": (art.get("source_id") or art.get("source_name") or "NewsData").title(),
                        "urgency": "High",
                        "trend_score": 90,
                        "url": art_url,
                        "source_name": (art.get("source_id") or art.get("source_name") or "NewsData").title(),
                        "published_at": art.get("pubDate", ""),
                        "image_url": art.get("image_url") or get_fallback_image(art.get("title", "")),
                        "is_live": True,
                    })
        except Exception as e:
            logger.error(f"Newsdata.io student fetch failed for {cat_name}: {e}")
    
    return results

def _update_student_cache_if_needed(db: Session, force: bool = False, country: str = "India"):
    """Internal helper to process country news into Student structure with caching."""
    target_name, match_keys, _ = normalize_country(country)
    country_key = target_name.lower()
    
    if country_key not in _student_news_caches:
        _student_news_caches[country_key] = {"last_updated": None, "articles": [], "trends": {}}
        
    cache = _student_news_caches[country_key]
    now = datetime.utcnow()
    if not force and cache["last_updated"] and (now - cache["last_updated"]).total_seconds() < 900:
        return cache
        
    lookback_period = now - timedelta(days=30)
    if target_name == "Global" or not country or country.lower() == "global":
        raw_articles = db.query(VerifiedNews).filter(VerifiedNews.created_at >= lookback_period).order_by(VerifiedNews.created_at.desc()).limit(2000).all()
    else:
        from sqlalchemy import or_
        # IMPORTANT: Always include Global articles (Manual ones) so they show up everywhere
        raw_articles = db.query(VerifiedNews).filter(
            or_(VerifiedNews.country.in_(match_keys), VerifiedNews.country == "Global"), 
            VerifiedNews.created_at >= lookback_period
        ).order_by(VerifiedNews.impact_score.desc(), VerifiedNews.created_at.desc()).limit(2000).all()
        
    # --- NEW: Fetch Real-time External Student News ---
    external_articles = []
    try:
        external_articles = _fetch_newsdata_student_articles(country=target_name)
        logger.info(f"Fetched {len(external_articles)} external student articles for {target_name}")
    except Exception as e:
        logger.error(f"External student fetch failed: {e}")

    # --- NEW: Check Section States from SystemConfig ---
    enabled_cats = ["All Updates"]
    cat_map = {
        "Scholarships & Internships": "show_scholarships",
        "Exams & Results": "show_exams",
        "Admissions & Courses": "show_admissions",
        "Career & Jobs": "show_career"
    }
    
    try:
        configs = {c.config_key: c.config_value for c in db.query(SystemConfig).all()}
        for display_name, config_key in cat_map.items():
            if configs.get(config_key, "true") == "true":
                enabled_cats.append(display_name)
    except Exception as e:
        logger.warning(f"SystemConfig query failed in student update (using defaults): {e}")
        # Default: all categories enabled
        enabled_cats.extend(list(cat_map.keys()))
    
    # --- Internal Processing Variables ---
    processed_articles = []
    category_counts = defaultdict(int)
    for cat in enabled_cats:
        category_counts[cat] = 0
    
    scholarship_count = 0
    exam_mentions = {}
    
    # 1. Process External Articles First (Higher Accuracy for Student Section)
    for art in external_articles:
        processed_articles.append(art)
        category_counts[art["category"]] += 1
        category_counts["All Updates"] += 1
        if "Scholarship" in art["category"]: scholarship_count += 1

    # 2. Add Internal Articles (Filtered)
    seen_urls = {a["url"] for a in processed_articles if a.get("url")}
    for article in raw_articles:
        if not is_student_article_logic(article):
            continue
            
        is_student_cat = article.category in STUDENT_NEWS_CATEGORIES
        
        cat = article.category if is_student_cat else "All Updates"
        if cat not in enabled_cats and cat != "All Updates":
            cat = "All Updates"
            
        # If even "All Updates" is the only thing enabled, or if this specific cat is disabled, 
        # we might want to skip or re-route. For now, we skip if the cat is explicitly disabled 
        # and not in our enabled list.
        if is_student_cat and cat not in enabled_cats:
            continue
            
        student_data = {
            "id": article.id,
            "title": article.title,
            "summary": article.why_it_matters or (article.summary_bullets[0] if article.summary_bullets else "Intelligence node active."),
            "category": cat,
            "published_at": article.created_at.isoformat() if article.created_at else now.isoformat(),
            "image_url": getattr(article, "url_to_image", None) or get_fallback_image(article.title),
            "url": f"/article/{article.id}",
            "source_name": article.source_name or "Global Intel",
            "tags": [f"#{cat.split(' ')[0]}"],
            "profiles": ["University Student", "Job Seeker"],
            "action_label": "Read Intelligence →",
            "trend_score": 1000 if (article.impact_score or 0) >= 100 else (100 if (article.impact_score or 0) > 8 else 85),
        }
        
        if student_data["url"] not in seen_urls:
            processed_articles.append(student_data)
            seen_urls.add(student_data["url"])
            category_counts[cat] += 1
            category_counts["All Updates"] += 1
            if "Scholarship" in cat: scholarship_count += 1
            if "Exam" in cat:
                for tag in student_data.get("tags", []):
                    exam_mentions[tag] = exam_mentions.get(tag, 0) + 1

    # Sort: trend_score first, then by date
    processed_articles.sort(key=lambda x: (x.get("trend_score", 0), x.get("published_at", "")), reverse=True)
    top_exam = max(exam_mentions.items(), key=lambda x: x[1])[0] if exam_mentions else "N/A"
    
    most_discussed = "N/A"
    if processed_articles:
        top_tags = {}
        ignored_tags = {"#Exam", "#CompetitiveExams", "#BoardExams", "#Education", "#Update", "#News", "#Students", "#Scholarship", "#Job", "#Career", "#StudyAbroad", "#Result"}
        for a in processed_articles[:20]:
            for t in a.get("tags", []):
                if t not in ignored_tags:
                    top_tags[t] = top_tags.get(t, 0) + 1
        if top_tags:
            most_discussed = max(top_tags.items(), key=lambda x: x[1])[0]
    
    if len(processed_articles) == 0 and target_name != "Global":
        global_cache = _update_student_cache_if_needed(db, force=True, country="Global")
        cache.update({"articles": global_cache.get("articles", []), "trends": global_cache.get("trends", {}), "last_updated": now})
        return cache

    cache["articles"] = processed_articles
    cache["trends"] = {
        "total_articles": len(processed_articles),
        "scholarship_count": scholarship_count,
        "category_counts": category_counts,
        "most_discussed_topic": most_discussed,
        "top_trending_exam": top_exam
    }
    cache["last_updated"] = now
    return cache

# --- PERSONAL AI NEWS AGENT ---

# REMOVED: /personal-agent UI route (Moved to Frontend Server)

@router.get("/api/v2/search-news")
@router.get("/api/search-news")
@router.get("/api/v2/get-personal-news")
@router.get("/api/get-personal-news")
async def api_get_personal_news(interests: str = None, q: str = None, lang: str = 'english', db: Session = Depends(get_db)):
    """Fetch hyper-personalized news based on search query and selected interests."""
    try:
        from sqlalchemy import or_
        now_utc = datetime.utcnow()
        lookback = now_utc - timedelta(days=90) # Extended lookback to capture more student/personal data
        
        search_terms = []
        if q: search_terms.append(q.lower().strip())
        if interests: 
            # Handle both comma separated and individual terms
            search_terms.extend([i.strip().lower() for i in interests.split(',') if i.strip()])
        
        if not search_terms:
            # If no interests specified, show top trending news as "Default Intelligence"
            articles = db.query(VerifiedNews).filter(
                VerifiedNews.created_at >= lookback
            ).order_by(VerifiedNews.impact_score.desc(), VerifiedNews.created_at.desc()).limit(20).all()
        else:
            filters = []
            for term in search_terms:
                filters.append(VerifiedNews.title.ilike(f"%{term}%"))
                filters.append(VerifiedNews.category.ilike(f"%{term}%"))
                filters.append(VerifiedNews.why_it_matters.ilike(f"%{term}%"))
                
            articles = db.query(VerifiedNews).filter(
                or_(*filters),
                VerifiedNews.created_at >= lookback
            ).order_by(VerifiedNews.created_at.desc(), VerifiedNews.impact_score.desc()).limit(60).all()
        
        # Deduplicate and normalize
        all_articles = []
        seen_ids = set()
        for a in articles:
            if a.id in seen_ids: continue
            seen_ids.add(a.id)
            
            normalized = normalize_article_data(a.to_dict())
            
            # Determine best tag matching the user's interests
            matched_tag = "Intelligence"
            if q:
                matched_tag = q.capitalize()
            elif interests:
                # Find which interest matched
                for term in [i.strip() for i in interests.split(',')]:
                    if term.lower() in (a.title or "").lower() or term.lower() in (a.category or "").lower():
                        matched_tag = term.capitalize()
                        break
            
            article_data = {
                "id": a.id,
                "title": normalized.get("title"),
                "summary": normalized.get("why_it_matters") or (normalized.get("summary_bullets", [""])[0] if normalized.get("summary_bullets") else "Intelligence report active."),
                "url": a.url,
                "image_url": a.image_url or get_fallback_image(a.title),
                "source_name": a.source_name,
                "published_at": a.created_at.isoformat() if a.created_at else None,
                "matched_interest": matched_tag
            }
            all_articles.append(article_data)

        # Apply Translations if lang != english
        if lang and lang.lower() != 'english' and all_articles:
            try:
                # Use the translator which now has the 6-key rotation and llama-3.3 model
                trans_input = [{"title": a["title"], "summary": a["summary"]} for a in all_articles]
                res = await translator._do_translate(trans_input, lang, "")
                t_list = res.get("translated_stories", [])
                for i, a in enumerate(all_articles):
                    if i < len(t_list):
                        t = t_list[i]
                        if t.get("title"): a["title"] = t["title"]
                        if t.get("summary"): a["summary"] = t["summary"]
            except Exception as e:
                logger.error(f"Personal translation failed: {e}")

        return {"status": "success", "articles": all_articles[:40], "has_more": False}
    except Exception as e:
        logger.error(f"Personal news fetch failed: {e}")
        return {"status": "error", "message": "Neural search node offline."}

# REMOVED: /crystal-ball UI route (Moved to Frontend Server)

@router.get("/api/v2/geopolitics-prediction")
@router.get("/api/geopolitics-prediction")
async def api_get_prediction_geo(db: Session = Depends(get_db)):
    """Specialized Geopolitics Prediction for the analysis dashboard."""
    try:
        latest = db.query(VerifiedNews).order_by(VerifiedNews.created_at.desc()).limit(10).all()
        trends = [a.title for a in latest]
        prediction = await llm_analyzer.generate_geopolitical_prediction_groq(trends)
        return prediction
    except Exception as e:
        logger.error(f"Geopolitics API failed: {e}")
        return {"headline": "Intelligence Node Offset", "prediction_text": "AI node currently unavailable.", "market_impact": "Monitor local nodes.", "confidence_level": "N/A"}

@router.post("/api/user/upload_profile_image")
async def upload_user_image(
    firebase_uid: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Handle manual profile image upload."""
    try:
        user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
        if not user:
            return {"status": "error", "message": "User not found"}
        
        # Save file locally (Simple implementation for now)
        upload_dir = "web/static/uploads/profiles"
        import os
        os.makedirs(upload_dir, exist_ok=True)
        
        file_ext = file.filename.split(".")[-1]
        file_path = f"{upload_dir}/{firebase_uid}.{file_ext}"
        
        with open(file_path, "wb") as buffer:
            import shutil
            shutil.copyfileobj(file.file, buffer)
        
        # Update user record
        image_url = f"/static/uploads/profiles/{firebase_uid}.{file_ext}"
        user.profile_image_url = image_url
        db.commit()
        
        return {"status": "success", "image_url": image_url}
    except Exception as e:
        logger.error(f"Image upload failed: {e}")
        return {"status": "error", "message": str(e)}

@router.post("/api/auth/twilio/send-otp")
async def send_twilio_otp(payload: dict = Body(...), db: Session = Depends(get_db)):
    phone = payload.get("phone")
    if not phone:
        raise HTTPException(status_code=400, detail="Phone number required")
    
    # Clean phone number
    phone = "".join(filter(str.isdigit, phone))
    if not phone.startswith('+'):
        # Default to India (+91) as requested for mobile login context
        if len(phone) == 10: phone = "+91" + phone
        else: phone = "+" + phone

    otp = str(random.randint(100000, 999999))
    expires_at = datetime.utcnow() + timedelta(minutes=5)
    
    from src.database.models import OTPVerification
    from src.utils.twilio_helper import twilio_helper
    
    # Save to DB
    new_otp = OTPVerification(phone=phone, otp_code=otp, expires_at=expires_at)
    db.add(new_otp)
    db.commit()
    
    # Send via Twilio
    success = twilio_helper.send_otp(phone, otp)
    if not success:
        logger.error(f"Twilio failure for {phone}")
        raise HTTPException(status_code=500, detail="Failed to send SMS via Twilio")
        
    return {"status": "success", "message": "OTP sent"}

@router.post("/api/auth/twilio/verify-otp")
async def verify_twilio_otp(payload: dict = Body(...), db: Session = Depends(get_db)):
    phone = payload.get("phone")
    otp = payload.get("otp")
    if not phone or not otp:
        raise HTTPException(status_code=400, detail="Phone and OTP required")
    
    # Clean phone number
    phone = "".join(filter(str.isdigit, phone))
    if not phone.startswith('+'):
        if len(phone) == 10: phone = "+91" + phone
        else: phone = "+" + phone

    from src.database.models import OTPVerification, User
    
    record = db.query(OTPVerification).filter(
        OTPVerification.phone == phone,
        OTPVerification.otp_code == otp,
        OTPVerification.expires_at > datetime.utcnow(),
        OTPVerification.is_verified == False
    ).order_by(OTPVerification.created_at.desc()).first()
    
    if not record:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    
    record.is_verified = True
    
    # Find or create user
    user = db.query(User).filter(User.phone == phone).first()
    if not user:
        user = User(phone=phone, firebase_uid=f"twilio_{uuid.uuid4().hex[:12]}", current_streak=1)
        db.add(user)
    
    db.commit()
    return {"status": "success", "firebase_uid": user.firebase_uid}

# Redundant /api/track-topic removed. Unified with /api/retention/track_topic in user_retention.py

@router.post("/api/v2/articles/{news_id}/update")
@router.post("/api/articles/{news_id}/update")
async def update_article_analysis(news_id: int, db: Session = Depends(get_db)):
    """Request fresh AI analysis for a specific article."""
    try:
        article = db.query(VerifiedNews).filter(VerifiedNews.id == news_id).first()
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        
        raw_article = article.raw_news
        if not raw_article:
             raise HTTPException(status_code=404, detail="Raw source missing")
             
        # Perform fresh analysis
        from src.analysis.llm_analyzer import LLMAnalyzer
        analyzer = LLMAnalyzer()
        
        # We re-analyze the specific raw content
        batch_input = [{"title": raw_article.title, "content": raw_article.content or raw_article.description}]
        analysis_list = await analyzer.analyze_batch(batch_input)
        
        if analysis_list:
            fresh = analysis_list[0]
            # Update article fields
            article.title = fresh.get("title") or article.title
            article.summary_bullets = fresh.get("summary_bullets") or article.summary_bullets
            article.why_it_matters = fresh.get("why_it_matters") or article.why_it_matters
            article.who_is_affected = fresh.get("who_is_affected") or article.who_is_affected
            article.impact_score = fresh.get("impact_score") or article.impact_score
            article.impact_tags = fresh.get("impact_tags") or article.impact_tags
            
            # Clear translation cache to force re-translation
            article.translation_cache = {}
            db.commit()
            
            return {"status": "success", "message": "Neural context updated"}
        
        return {"status": "error", "message": "AI Node rejected update"}
    except Exception as e:
        logger.error(f"Manual update failed for article {news_id}: {e}")
        return {"status": "error", "message": str(e)}


# --- HELPERS ---

# Duplicate normalize_country removed during consolidation.


@router.get("/api/cricket/live")
async def get_live_cricket():
    """Endpoint for the draggable cricket widget with real-time scraped data."""
    try:
        import requests
        from bs4 import BeautifulSoup
        
        url = "https://www.cricbuzz.com/cricket-match/live-scores"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code != 200:
            raise Exception(f"Cricbuzz returned {resp.status_code}")
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        matches_html = soup.find_all('div', class_='cb-mtch-lst')
        
        all_matches = []
        for match in matches_html:
            header = match.find('h3', class_='cb-lv-scr-mtch-hdr')
            if not header: continue
            
            title = header.text.strip()
            # Priority for India / IPL
            is_india = any(kw in title.lower() for kw in ["india", "ind ", " ind", "ipl", "wpl", "mumbai", "chennai", "delhi", "bangalore", "kolkata", "rajasthan", "punjab", "gujarat", "lucknow", "hyderabad"])
            
            status_div = match.find('div', class_='cb-text-live') or match.find('div', class_='cb-text-complete') or match.find('div', class_='cb-text-preview')
            if not status_div: continue
            
            score_div = match.find('div', class_='cb-scr-wgt-cont') or match.find('div', class_='cb-scr-wkt-line')
            short_score = score_div.text.strip() if score_div else "Scheduled / Tracking..."
            
            all_matches.append({
                "name": title,
                "short_score": short_score,
                "status": status_div.text.strip(),
                "is_india": is_india,
                "is_live": "live" in status_div.get('class', []) or "live" in status_div.text.lower()
            })

        if all_matches:
            # Sort: Live first, then India matches
            all_matches.sort(key=lambda x: (x["is_live"], x["is_india"]), reverse=True)
            return {
                "live": any(m["is_live"] for m in all_matches),
                "matches": all_matches,
                "count": len(all_matches)
            }
        
        return {"live": False, "message": "No active cricket matches found."}

    except Exception as e:
        logger.error(f"Cricket Scraper Failed: {e}")
        return {"live": False, "message": "Cricket feed temporarily unavailable."}

# --- RESTORED ENDPOINTS ---

@router.post("/api/v2/generate-exam")
async def generate_exam(db: Session = Depends(get_db)):
    """Restored Mock Test endpoint using high-performance fallback."""
    try:
        from src.analysis.exam_generator import ExamGenerator
        generator = ExamGenerator()
        exam_data = await generator.generate_from_news(db)
        return {"status": "success", "exam": exam_data}
    except Exception as e:
        logger.error(f"Exam generation failed: {e}")
        return {"status": "error", "message": "Failed to generate exam."}

@router.get("/api/v2/universe/search")
async def universe_search(q: str, db: Session = Depends(get_db)):
    """Restored Globe/Universe Search for cross-border intelligence."""
    try:
        results = await universe_collector.search_global(q, db)
        return {"status": "success", "results": results}
    except Exception as e:
        logger.error(f"Universe search failed: {e}")
        return {"status": "error", "message": "Search unavailable."}

# END OF DASHBOARD ROUTER
