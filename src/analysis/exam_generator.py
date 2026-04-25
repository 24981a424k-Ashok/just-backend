import json
import logging
import random
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import List, Dict
from sqlalchemy.orm import Session
from src.database.models import VerifiedNews, DailyDigest
from src.analysis.llm_analyzer import LLMAnalyzer

class ExamGenerator:
    def __init__(self):
        self.llm = LLMAnalyzer()

    def get_recent_news(self, db: Session) -> List[Dict]:
        """Fetch all verified news from the last 24 hours for total accuracy."""
        now = datetime.utcnow()
        last_24h = now - timedelta(hours=24)
        
        # Perfection: Increased fetch to 30 items for better accuracy and selection
        news = db.query(VerifiedNews).filter(
            VerifiedNews.created_at >= last_24h,
            VerifiedNews.impact_score >= 4
        ).order_by(VerifiedNews.impact_score.desc()).limit(30).all()
        
        return [n.to_dict() for n in news]


    async def generate_from_news(self, db: Session) -> Dict:
        """Generate a high-accuracy mock test from the last 24 hours of intelligence."""
        news_items = self.get_recent_news(db)
        
        if not news_items:
            # Fallback to latest digest if no specific window news found
            latest = db.query(DailyDigest).order_by(DailyDigest.date.desc()).first()
            if latest:
                news_items = latest.content_json.get('top_stories', [])

        if not news_items:
            # Absolute fallback to latest 15 verified news regardless of time
            news = db.query(VerifiedNews).order_by(VerifiedNews.created_at.desc()).limit(15).all()
            news_items = [n.to_dict() for n in news]

        if not news_items:
            return {
                "title": "System Initializing",
                "questions": [],
                "error": "Intelligence scan found no fresh news. Please run a news cycle first."
            }

        # Perfection: Ensure diverse category representation
        categorized_news = defaultdict(list)
        for n in news_items:
            cat = (n.get('category') or 'General').strip().capitalize()
            categorized_news[cat].append(n)
        
        # Select balanced items (Aim for 4 items per major category)
        balanced_items = []
        major_cats = ['National', 'International', 'Economy', 'Science', 'Sports']
        for cat in major_cats:
            items = categorized_news.get(cat, [])
            random.shuffle(items)
            balanced_items.extend(items[:4])
            
        remaining = [n for n in news_items if n not in balanced_items]
        random.shuffle(remaining)
        balanced_items.extend(remaining)
        
        # Use balanced selection for text context
        news_text = ""
        for n in balanced_items[:25]: 
            title = n.get('title', 'Unknown')
            why = n.get('why_it_matters') or n.get('why', '')
            bullets = ", ".join(n.get('summary_bullets') or n.get('bullets', []))
            cat = n.get('category', 'General')
            news_text += f"ARTICLE: {title}\nFACTS: {bullets}\nIMPACT: {why}\nCATEGORY: {cat}\n\n"

        logging.info(f"Generating balanced questions from {len(balanced_items)} nodes across {len(categorized_news)} categories.")

        
        prompt = f"""
        You are an AI Current Affairs Exam Expert specializing in Indian and global competitive exams (UPSC, SSC, Banking).
        
        Create a Daily Current Affairs Mock Test based on the following news:
        {news_text}
        
        RULES:
        1. Generate exactly 15 questions.
        2. Format: 
           - 9 MCQs (4 options)
           - 3 Statement-based (as MCQs with options like "Only 1", "Both 1 and 2")
           - 2 Match the Following (formatted as MCQ: "Which pair is correctly matched?" or "Choose the correct sequence")
           - 1 True/False
        3. Sections: National, International, Economy, Science, Sports.
        4. Output JSON format ONLY:
        {{
            "title": "Daily Mock Test - {datetime.now().strftime('%Y-%m-%d')}",
            "questions": [
                {{
                    "id": 1,
                    "type": "MCQ",
                    "section": "National Affairs",
                    "question": "...",
                    "options": ["A", "B", "C", "D"],
                    "correct_answer": "A",
                    "explanation": "..."
                }}
            ]
        }}
        """
        
        try:
            # We use asyncio to run the LLM completion if it's blocking, but usually analyzers have their own async methods if needed.
            # For now, we'll keep it simple as the web dashboard calls it with await.
            response = self.llm.get_completion(prompt)
            # Clean JSON if needed
            response = response.replace("```json", "").replace("```", "").strip()
            data = json.loads(response)
            if "questions" not in data:
                data["questions"] = []
            return data
        except Exception as e:
            logging.error(f"Exam Generation Error: {e}")
            
            # Fallback: Load from question bank
            try:
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                bank_path = os.path.join(base_dir, 'data', 'question_bank.json')
                
                if os.path.exists(bank_path):
                    with open(bank_path, 'r', encoding='utf-8') as f:
                        all_questions = json.load(f)
                    
                    selected_questions = random.sample(all_questions, min(len(all_questions), 15))
                    for idx, q in enumerate(selected_questions):
                        q['id'] = idx + 1
                        
                    return {
                        "title": f"Daily Mock Test - General Awareness - {datetime.now().strftime('%d %b %Y')}",
                        "questions": selected_questions
                    }
                else:
                    raise FileNotFoundError("Bank missing")

            except Exception as bank_error:
                fallback_questions = [
                    {
                        "id": 1,
                        "type": "MCQ",
                        "section": "General",
                        "question": "Which organization releases the 'World Economic Outlook'?",
                        "options": ["IMF", "World Bank", "WEF", "ADB"],
                        "correct_answer": "IMF",
                        "explanation": "The IMF releases the WEO report."
                    },
                    {
                        "id": 2,
                        "type": "MCQ",
                        "section": "Science",
                        "question": "Which NASA mission recently returned asteroid samples to Earth?",
                        "options": ["OSIRIS-REx", "Juno", "Artemis", "New Horizons"],
                        "correct_answer": "OSIRIS-REx",
                        "explanation": "OSIRIS-REx returned samples from asteroid Bennu in 2023."
                    },
                    {
                        "id": 3,
                        "type": "MCQ",
                        "section": "National",
                        "question": "India's G20 Presidency theme was:",
                        "options": ["One Earth One Family", "Digital India", "Vasudhaiva Kutumbakum", "Atmanirbhar Bharat"],
                        "correct_answer": "Vasudhaiva Kutumbakum",
                        "explanation": "The theme was Vasudhaiva Kutumbakum or One Earth One Family One Future."
                    },
                    {
                        "id": 4,
                        "type": "MCQ",
                        "section": "Sports",
                        "question": "Who won the Men's ODI World Cup 2023?",
                        "options": ["India", "Australia", "England", "New Zealand"],
                        "correct_answer": "Australia",
                        "explanation": "Australia won their 6th title in 2023."
                    },
                    {
                        "id": 5,
                        "type": "MCQ",
                        "section": "Economy",
                        "question": "What is the primary focus of the PM-KUSUM scheme?",
                        "options": ["Education", "Solar Energy for Farmers", "Railways", "Textiles"],
                        "correct_answer": "Solar Energy for Farmers",
                        "explanation": "PM-KUSUM focuses on solar energy and water security for farmers."
                    }
                ]
                
                random.shuffle(fallback_questions)
                for idx, q in enumerate(fallback_questions):
                    q['id'] = idx + 1

                return {
                    "title": f"Daily Mock Test (Smart Fallback) - {datetime.now().strftime('%d %b %Y')}",
                    "questions": fallback_questions
                }

