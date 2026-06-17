from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from typing import Optional
import uuid
import logging
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth

from database.connection import Base, engine, get_db
from database.models import User, ConversationLog, Company
from agent.redis_memory import redis_memory
from agent.gemini_agent import GeminiAgent
from routers import whatsapp, cron
from config.settings import settings

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Try to initialize Firebase Admin (will fail gracefully if no credentials exist)
try:
    firebase_admin.initialize_app()
except ValueError:
    pass
except Exception as e:
    logger.warning(f"Firebase not initialized: {e}")

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

# Startup DB migration helper
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

# Pydantic schemas
class CompanyCreate(BaseModel):
    name: str

class SellerPreRegister(BaseModel):
    name: str
    phone_number: str
    email: EmailStr
    sales_goals: Optional[str] = None
    objectives: Optional[str] = None

class SellerClaim(BaseModel):
    company_code: str
    email: EmailStr
    firebase_uid: str
    google_refresh_token: str

class SellerIndependent(BaseModel):
    name: str
    email: EmailStr
    phone_number: str
    firebase_uid: str
    google_refresh_token: str
    spreadsheet_id: Optional[str] = None
    template_doc_id: Optional[str] = None
    sales_goals: Optional[str] = None
    objectives: Optional[str] = None

class ChatRequest(BaseModel):
    phone_number: str
    message: str

# Endpoints
@app.post("/companies", status_code=201)
def register_company(payload: CompanyCreate, db: Session = Depends(get_db)):
    code = f"{payload.name[:3].upper()}-{str(uuid.uuid4())[:6].upper()}"
    new_company = Company(name=payload.name, company_code=code)
    db.add(new_company)
    db.commit()
    db.refresh(new_company)
    return {"id": new_company.id, "name": new_company.name, "company_code": new_company.company_code}

@app.post("/companies/{company_code}/sellers", status_code=201)
def preregister_seller(company_code: str, payload: SellerPreRegister, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.company_code == company_code).first()
    if not company:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    
    new_user = User(
        name=payload.name,
        phone_number=payload.phone_number,
        email=payload.email,
        role="vendedor_empresa",
        company_id=company.id,
        sales_goals=payload.sales_goals,
        objectives=payload.objectives
    )
    db.add(new_user)
    db.commit()
    return {"detail": "Vendedor pre-registrado correctamente"}

@app.post("/auth/seller/claim")
def claim_account(payload: SellerClaim, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.company_code == payload.company_code).first()
    if not company:
        raise HTTPException(status_code=404, detail="Código de empresa inválido")
    
    user = db.query(User).filter(User.company_id == company.id, User.email == payload.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="No se encontró un pre-registro para este email asociado a la empresa.")
    
    user.firebase_uid = payload.firebase_uid
    user.set_refresh_token(payload.google_refresh_token)
    db.commit()
    return {"detail": "Cuenta vinculada exitosamente", "phone_number": user.phone_number}

@app.post("/auth/seller/independent")
def register_independent(payload: SellerIndependent, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter((User.email == payload.email) | (User.phone_number == payload.phone_number)).first()
    if existing_user:
         raise HTTPException(status_code=400, detail="Ya existe un vendedor con este correo o teléfono.")
         
    new_user = User(
        name=payload.name,
        email=payload.email,
        phone_number=payload.phone_number,
        firebase_uid=payload.firebase_uid,
        role="vendedor_independiente",
        spreadsheet_id=payload.spreadsheet_id,
        template_doc_id=payload.template_doc_id,
        sales_goals=payload.sales_goals,
        objectives=payload.objectives
    )
    new_user.set_refresh_token(payload.google_refresh_token)
    db.add(new_user)
    db.commit()
    return {"detail": "Vendedor independiente registrado", "phone_number": new_user.phone_number}

@app.post("/agent/chat")
def agent_chat(payload: ChatRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.phone_number == payload.phone_number).first()
    if not user:
        raise HTTPException(status_code=404, detail="Vendedor no encontrado.")
    
    try:
        refresh_token = user.get_refresh_token()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Error de tokens de Google.")

    chat_history = redis_memory.get_history(payload.phone_number)
    
    agent = GeminiAgent(
        user_refresh_token=refresh_token or "",
        spreadsheet_id=user.spreadsheet_id,
        template_doc_id=user.template_doc_id,
        sales_goals=user.sales_goals,
        objectives=user.objectives
    )

    reply, updated_history = agent.run(chat_history, payload.message)

    redis_memory.add_message(payload.phone_number, "user", payload.message)
    redis_memory.add_message(payload.phone_number, "agent", reply)

    db.add(ConversationLog(phone_number=payload.phone_number, sender="user", message=payload.message))
    db.add(ConversationLog(phone_number=payload.phone_number, sender="agent", message=reply))
    db.commit()

    return {"reply": reply}

@app.get("/users")
def list_users(db: Session = Depends(get_db)):
    users = db.query(User).all()
    return [{"phone_number": u.phone_number, "name": u.name, "email": u.email, "role": u.role} for u in users]

@app.get("/", response_class=HTMLResponse)
def read_root():
    import os
    file_path = os.path.join(os.path.dirname(__file__), "frontend.html")
    with open(file_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())
