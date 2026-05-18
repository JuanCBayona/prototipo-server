import os
import re
import fitz  # PyMuPDF
from typing import List, Optional
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, Body
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
import firebase_admin
from firebase_admin import credentials, messaging

# ==========================================
# 1. DATABASE CONFIGURATION
# ==========================================
# Defaults to SQLite for instant deployment, automatically upgrades to Postgres if URL is provided by Render
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./kishnapp_cloud.db")

# Render uses 'postgres://', but SQLAlchemy requires 'postgresql://'
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ==========================================
# 2. SQLALCHEMY MODELS
# ==========================================
class SongDB(Base):
    __tablename__ = "songs"
    id = Column(Integer, primary_key=True, index=True)
    category = Column(String, index=True)  # NEW FIELD
    title = Column(String, index=True)
    lyrics = Column(String)

class EventDB(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    date = Column(String)
    time = Column(String)
    location = Column(String)
    resources = relationship("ResourceDB", back_populates="event", cascade="all, delete")

class ResourceDB(Base):
    __tablename__ = "resources"
    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"))
    name = Column(String)
    quantity = Column(Float)
    unit = Column(String)
    event = relationship("EventDB", back_populates="resources")

Base.metadata.create_all(bind=engine)

# ==========================================
# 3. PYDANTIC SCHEMAS (Data Validation)
# ==========================================
class ResourceSchema(BaseModel):
    name: str
    quantity: float
    unit: str

class EventCreate(BaseModel):
    title: str
    date: str
    time: str
    location: str
    resources: List[ResourceSchema]

class RSVPRequest(BaseModel):
    attendees: int

class BroadcastRequest(BaseModel):
    item_name: str

# ==========================================
# 4. FASTAPI APP & FIREBASE INIT
# ==========================================
app = FastAPI(title="Kishnapp API", version="1.0")

# Initialize Firebase ONLY if the key exists (prevents crash on first Render deploy)
firebase_initialized = False
try:
    if os.path.exists("serviceAccountKey.json"):
        cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred)
        firebase_initialized = True
except Exception as e:
    print(f"Firebase Init Skipped: {e}")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==========================================
# 5. ENDPOINTS
# ==========================================

@app.get("/api/sync")
def sync_initial_data(db: Session = Depends(get_db)):
    """Cold Start Data Synchronization"""
    events = db.query(EventDB).all()
    songs = db.query(SongDB).all()
    # Ekadashi dates would theoretically be fetched from a Vaishnava calendar API, 
    # returning a static list here for prototype purposes.
    return {
        "upcoming_ekadashi": ["2026-05-23: Mohini Ekadashi", "2026-06-07: Apara Ekadashi"],
        "events": events,
        "kirtan_count": len(songs)
    }

@app.post("/api/events")
def create_event(event: EventCreate, db: Session = Depends(get_db)):
    """Creates an event and its associated resources"""
    new_event = EventDB(title=event.title, date=event.date, time=event.time, location=event.location)
    db.add(new_event)
    db.commit()
    db.refresh(new_event)
    
    for res in event.resources:
        new_resource = ResourceDB(event_id=new_event.id, name=res.name, quantity=res.quantity, unit=res.unit)
        db.add(new_resource)
    
    db.commit()
    return {"message": "Event created successfully", "event_id": new_event.id}

@app.post("/api/events/{event_id}/rsvp")
def rsvp_to_event(event_id: int, rsvp: RSVPRequest, db: Session = Depends(get_db)):
    """Dynamic Resource Scaling Algorithm based on RSVP attendees"""
    resources = db.query(ResourceDB).filter(ResourceDB.event_id == event_id).all()
    if not resources:
        raise HTTPException(status_code=404, detail="Event or resources not found")
    
    # Pre-defined per-capita multiplier rule (e.g., 0.15 for scaling)
    MULTIPLIER = 0.15 
    added_factor = rsvp.attendees * MULTIPLIER
    
    for res in resources:
        # Update the required limit dynamically
        res.quantity += added_factor
    
    db.commit()
    
    # Return updated resources to instantly refresh the Android UI
    updated = db.query(ResourceDB).filter(ResourceDB.event_id == event_id).all()
    return {"message": f"RSVP confirmed for {rsvp.attendees}", "updated_resources": updated}

@app.post("/api/songs/upload")
async def upload_pdf(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Automated PDF Pipeline: Parses Sections and Songs via Font Heuristics"""
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Must be a PDF file")

    pdf_bytes = await file.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    songs_data = []
    current_category = "General" # Fallback category
    current_title = ""
    current_lyrics = ""

    # Thresholds (Tuned for your specific Vaishnava Songbook)
    SECTION_SIZE_THRESHOLD = 15.0  # Huge text (e.g., "Vandana")
    TITLE_SIZE_THRESHOLD = 13.0    # Large text (e.g., "(1) Mangalacarana")

    for page in doc:
        # SKIP THE INDEX PAGES: We ignore pages 1-4 so the parser 
        # doesn't try to read the Table of Contents as actual songs.
        # PyMuPDF is 0-indexed, so page index < 4 skips pages 1, 2, 3, and 4.
        if page.number < 4:
            continue

        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        
        for b in blocks:
            if b['type'] == 0:
                for line in b["lines"]:
                    for span in line["spans"]:
                        text = span["text"].strip()
                        
                        # Skip empty lines and page numbers
                        if not text or text.isdigit():
                            continue
                            
                        font_size = span["size"]
                        
                        # DETECT CATEGORY: Is it huge text?
                        if font_size >= SECTION_SIZE_THRESHOLD:
                            # Set the new category for all subsequent songs
                            current_category = text.title()
                            continue
                        
                        # DETECT SONG TITLE: Is it large text?
                        elif font_size >= TITLE_SIZE_THRESHOLD:
                            if current_title and current_lyrics:
                                songs_data.append({
                                    "category": current_category.strip(),
                                    "title": current_title.strip(),
                                    "lyrics": current_lyrics.strip()
                                })
                                current_lyrics = ""
                            
                            # Clean up numerical prefixes "(1) " from the title
                            clean_title = re.sub(r'^[\(\d\)\.\-\s]+', '', text)
                            current_title = clean_title
                        
                        # DETECT LYRICS: Normal body text
                        else:
                            if current_title: # Only add lyrics if we have a title active
                                current_lyrics += text + "\n"
                            
                if current_lyrics and not current_lyrics.endswith("\n\n"):
                    current_lyrics += "\n"

    # Catch the final song
    if current_title and current_lyrics:
        songs_data.append({"category": current_category.strip(), "title": current_title.strip(), "lyrics": current_lyrics.strip()})

    # Wipe old database to prevent duplicates
    db.query(SongDB).delete()
    db.commit()

    saved_count = 0
    for song in songs_data:
        clean_lyrics = re.sub(r'\n{3,}', '\n\n', song["lyrics"])
        if song["title"] and clean_lyrics:
            new_song = SongDB(category=song["category"], title=song["title"], lyrics=clean_lyrics)
            db.add(new_song)
            saved_count += 1
        
    db.commit()
    return {"message": "Parsed via Two-Tier Heuristics", "songs_extracted": saved_count}

@app.get("/api/songs")
def get_songs(db: Session = Depends(get_db)):
    """Client Synchronization for Offline Display"""
    return db.query(SongDB).all()

@app.post("/api/urgent_broadcast")
def send_urgent_broadcast(req: BroadcastRequest = Body(...)):
    """Pushes a multicast FCM notification to all users"""
    if not firebase_initialized:
        raise HTTPException(status_code=503, detail="Firebase Admin SDK not initialized on server")
    
    message = messaging.MulticastMessage(
        data={
            "type": "URGENT_NEED",
            "item_name": req.item_name
        },
        topic="All_Users"
    )
    response = messaging.send_multicast(message)
    return {"message": "Broadcast sent", "success_count": response.success_count}
