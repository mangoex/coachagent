from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from typing import Optional

from database.connection import Base, engine, get_db
from database.models import User
from routers import whatsapp, cron
from config.settings import settings

import logging

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Initialize FastAPI App
app = FastAPI(
    title="Google AI Sales Coach Agent API",
    description="Automated Sales Coaching, CRM Synchronization, and Proposal Generator agent.",
    version="1.0.0"
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Startup DB migration helper (auto-creates tables if they don't exist)
@app.on_event("startup")
def startup_event():
    logger.info("Initializing database tables...")
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables initialized successfully.")
    except Exception as e:
        logger.critical(f"Failed to initialize database tables: {str(e)}")

# Include Routers
app.include_router(whatsapp.router)
app.include_router(cron.router)

# Pydantic schemas for user registration
class UserCreate(BaseModel):
    email: EmailStr
    name: str
    phone_number: str  # Format: "52155..." or "+1..."
    google_refresh_token: str
    spreadsheet_id: Optional[str] = None
    template_doc_id: Optional[str] = None

@app.get("/")
def read_root():
    return {
        "status": "healthy",
        "service": "Google AI Sales Coach Agent Backend",
        "environment": settings.ENVIRONMENT
    }

@app.post("/users", status_code=210)
def register_user(user_in: UserCreate, db: Session = Depends(get_db)):
    """
    Registers a new salesperson/tenant and encrypts their Google OAuth 2.0 refresh token.
    """
    existing_user = db.query(User).filter(
        (User.email == user_in.email) | (User.phone_number == user_in.phone_number)
    ).first()
    
    if existing_user:
        raise HTTPException(
            status_code=400,
            detail="A user with this email or phone number is already registered."
        )

    db_user = User(
        email=user_in.email,
        name=user_in.name,
        phone_number=user_in.phone_number,
        spreadsheet_id=user_in.spreadsheet_id,
        template_doc_id=user_in.template_doc_id
    )
    # Encrypt the refresh token before storing
    db_user.set_refresh_token(user_in.google_refresh_token)
    
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    
    logger.info(f"Registered new user: {db_user.email}")
    return {
        "id": db_user.id,
        "name": db_user.name,
        "email": db_user.email,
        "phone_number": db_user.phone_number,
        "spreadsheet_id": db_user.spreadsheet_id,
        "template_doc_id": db_user.template_doc_id
    }
