from fastapi import FastAPI, Depends, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel, EmailStr
from typing import Optional
import uuid
import logging
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth
from google_auth_oauthlib.flow import Flow

from database.connection import Base, engine, get_db
from database.models import User, ConversationLog, Company
from agent.redis_memory import redis_memory
from agent.gemini_agent import GeminiAgent
from routers import whatsapp, cron, audit
from config.settings import settings
from services.calendar_service import GoogleCalendarService

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
        # Hotfix: add new columns to existing users table
        with engine.connect() as conn:
            migrations = [
                "ALTER TABLE users ADD COLUMN firebase_uid VARCHAR;",
                "ALTER TABLE users ADD COLUMN role VARCHAR DEFAULT 'vendedor_independiente';",
                "ALTER TABLE users ADD COLUMN company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL;",
                "ALTER TABLE users ADD COLUMN sales_goals TEXT;",
                "ALTER TABLE users ADD COLUMN objectives TEXT;",
                "ALTER TABLE users ADD COLUMN calendar_id VARCHAR DEFAULT 'primary';",
                "ALTER TABLE companies ADD COLUMN whatsapp_phone_number_id VARCHAR;",
                "ALTER TABLE companies ADD COLUMN encrypted_whatsapp_token VARCHAR;",
                "ALTER TABLE companies ADD COLUMN global_goals TEXT;"
            ]
            for query in migrations:
                try:
                    conn.execute(text(query))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    logger.info(f"Migration skipped (likely exists): {e}")
        logger.info("Database tables initialized successfully.")
    except Exception as e:
        logger.critical(f"Failed to initialize database tables: {str(e)}")

# Include Routers
app.include_router(whatsapp.router)
app.include_router(cron.router)
app.include_router(audit.router)

# Pydantic schemas
class CompanyCreate(BaseModel):
    name: str

class CompanyUpdate(BaseModel):
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_token: Optional[str] = None

class GlobalGoalsUpdate(BaseModel):
    global_goals: str

class AIGoalProposalRequest(BaseModel):
    metrics: dict

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
    google_refresh_token: Optional[str] = None

class SellerIndependent(BaseModel):
    name: str
    email: EmailStr
    phone_number: str
    firebase_uid: str
    google_refresh_token: Optional[str] = None
    spreadsheet_id: Optional[str] = None
    template_doc_id: Optional[str] = None
    sales_goals: Optional[str] = None
    objectives: Optional[str] = None

class ChatRequest(BaseModel):
    phone_number: str
    message: str

class SellerUpdate(BaseModel):
    name: Optional[str] = None
    phone_number: Optional[str] = None
    sales_goals: Optional[str] = None

class GoogleTokenUpdate(BaseModel):
    google_refresh_token: str

# Setup OAuth Flow helper
SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

def get_client_secrets():
    return {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID.strip() if settings.GOOGLE_CLIENT_ID else None,
            "project_id": settings.GCP_PROJECT_ID,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": settings.GOOGLE_CLIENT_SECRET.strip() if settings.GOOGLE_CLIENT_SECRET else None,
            "redirect_uris": []
        }
    }

# Endpoints
@app.post("/companies", status_code=201)
def register_company(payload: CompanyCreate, db: Session = Depends(get_db)):
    code = f"{payload.name[:3].upper()}-{str(uuid.uuid4())[:6].upper()}"
    new_company = Company(name=payload.name, company_code=code)
    db.add(new_company)
    db.commit()
    db.refresh(new_company)
    return {"id": new_company.id, "name": new_company.name, "company_code": new_company.company_code}

@app.get("/companies/{company_code}")
def get_company(company_code: str, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.company_code == company_code).first()
    if not company:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    return {
        "id": company.id, 
        "name": company.name, 
        "company_code": company.company_code,
        "whatsapp_phone_number_id": company.whatsapp_phone_number_id,
        "has_whatsapp_token": bool(company.encrypted_whatsapp_token)
    }

@app.put("/companies/{company_code}")
def update_company(company_code: str, payload: CompanyUpdate, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.company_code == company_code).first()
    if not company:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    
    if payload.whatsapp_phone_number_id is not None:
        company.whatsapp_phone_number_id = payload.whatsapp_phone_number_id
    if payload.whatsapp_token is not None:
        company.set_whatsapp_token(payload.whatsapp_token)
        
    db.commit()
    return {"status": "ok"}

@app.get("/companies/{company_code}/sellers")
def list_company_sellers(company_code: str, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.company_code == company_code).first()
    if not company:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    
    sellers = db.query(User).filter(User.company_id == company.id).all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "email": s.email,
            "phone_number": s.phone_number,
            "role": s.role,
            "sales_goals": s.sales_goals,
            "objectives": s.objectives
        } for s in sellers
    ]

@app.put("/companies/{company_code}/global-goals")
def update_global_goals(company_code: str, payload: GlobalGoalsUpdate, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.company_code == company_code).first()
    if not company:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    
    company.global_goals = payload.global_goals
    db.commit()
    return {"status": "ok", "global_goals": company.global_goals}

@app.get("/companies/{company_code}/dashboard")
def get_dashboard_metrics(company_code: str, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.company_code == company_code).first()
    if not company:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    
    sellers = db.query(User).filter(User.company_id == company.id).all()
    
    # Mock data generation based on sellers for MVP visualization
    # Real data would come from reading CRM Sheets
    import random
    
    metrics_list = []
    total_sales = 0
    total_target = 0
    
    for s in sellers:
        # Generate stable mock data using user ID as seed
        random.seed(s.id + 100) 
        target = random.randint(10000, 50000)
        sales = int(target * random.uniform(0.4, 1.1))
        conv_rate = round(random.uniform(15, 45), 1)
        roi = round(random.uniform(1.2, 3.5), 1)
        clients = random.randint(20, 150)
        
        metrics_list.append({
            "id": s.id,
            "name": s.name,
            "role": s.role,
            "sales_goals": s.sales_goals,
            "metrics": {
                "sales": sales,
                "target": target,
                "conversion_rate": conv_rate,
                "roi": roi,
                "clients": clients
            }
        })
        
        total_sales += sales
        total_target += target
        
    return {
        "global_goals": company.global_goals,
        "company_name": company.name,
        "aggregated": {
            "total_sales": total_sales,
            "total_target": total_target,
            "avg_conversion": round(sum([m["metrics"]["conversion_rate"] for m in metrics_list]) / len(metrics_list), 1) if metrics_list else 0,
            "avg_roi": round(sum([m["metrics"]["roi"] for m in metrics_list]) / len(metrics_list), 1) if metrics_list else 0
        },
        "sellers": metrics_list
    }

@app.post("/companies/{company_code}/sellers/{seller_id}/ai-goals")
def suggest_ai_goals(company_code: str, seller_id: int, payload: AIGoalProposalRequest, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.company_code == company_code).first()
    if not company:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
        
    seller = db.query(User).filter(User.id == seller_id, User.company_id == company.id).first()
    if not seller:
        raise HTTPException(status_code=404, detail="Vendedor no encontrado en esta empresa")
        
    try:
        from agent.gemini_agent import GenerativeModel
        model = GenerativeModel(model_name="gemini-2.5-pro")
        
        prompt = f"""
        Eres un analista de ventas experto. La empresa '{company.name}' tiene las siguientes Metas Globales:
        {company.global_goals or 'No definidas explícitamente, asume maximizar ingresos y retención.'}
        
        El vendedor '{seller.name}' tiene este rendimiento actual:
        - Ventas Logradas: ${payload.metrics.get('sales', 0)}
        - Clientes Atendidos: {payload.metrics.get('clients', 0)}
        - Tasa de Conversión: {payload.metrics.get('conversion_rate', 0)}%
        - ROI: {payload.metrics.get('roi', 0)}x
        
        Genera una propuesta de "Metas de Venta" personalizadas para este vendedor (máximo 3 párrafos cortos).
        Deben ser accionables, alineadas a las metas globales de la empresa, pero considerando su rendimiento actual.
        No uses saludos ni introducciones, responde directamente con las metas.
        """
        
        response = model.generate_content(prompt)
        return {"suggested_goals": response.text.strip()}
    except Exception as e:
        logger.error(f"Error calling AI: {e}")
        return {"suggested_goals": f"Error al generar propuesta con IA. Revisa las configuraciones de Vertex AI. Detalle: {str(e)}"}

@app.put("/seller/{user_id}/goals")
def update_seller_goals(user_id: int, payload: SellerUpdate, db: Session = Depends(get_db)):
    seller = db.query(User).filter(User.id == user_id).first()
    if not seller:
        raise HTTPException(status_code=404, detail="Vendedor no encontrado")
    
    if payload.sales_goals is not None:
        seller.sales_goals = payload.sales_goals
    
    db.commit()
    return {"status": "ok", "sales_goals": seller.sales_goals}

@app.put("/auth/seller/{user_id}/google")
def update_google_token(user_id: int, payload: GoogleTokenUpdate, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    user.set_refresh_token(payload.google_refresh_token)
    db.commit()
    return {"status": "ok"}

@app.get("/api/sellers/{user_id}/upcoming")
def get_upcoming_events(user_id: int, db: Session = Depends(get_db)):
    from services.calendar_service import GoogleCalendarService
    seller = db.query(User).filter(User.id == user_id).first()
    if not seller:
        raise HTTPException(status_code=404, detail="Vendedor no encontrado")
    
    refresh_token = seller.get_refresh_token()
    if not refresh_token:
        return {"events": [], "error": "No hay Google Calendar vinculado."}
    
    try:
        events = GoogleCalendarService.get_upcoming_events(refresh_token, days_ahead=7)
        return {"events": events}
    except Exception as e:
        print("Error fetching calendar:", e)
        return {"events": [], "error": "Error al conectar con Google Calendar."}

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

class CompanyAdminCreate(BaseModel):
    company_name: str
    admin_name: str
    phone_number: str
    email: str
    firebase_uid: str

@app.post("/auth/seller/company-admin")
def register_company_admin(payload: CompanyAdminCreate, db: Session = Depends(get_db)):
    # Check if phone or email already exists
    if db.query(User).filter(User.phone_number == payload.phone_number).first():
        raise HTTPException(status_code=400, detail="Este número de teléfono ya está registrado con otra cuenta.")
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Este correo ya está registrado en nuestra base de datos.")

    # Create Company
    code = f"{payload.company_name[:3].upper()}-{str(uuid.uuid4())[:6].upper()}"
    new_company = Company(name=payload.company_name, company_code=code)
    db.add(new_company)
    db.commit()
    db.refresh(new_company)
    
    # Create Admin User
    new_user = User(
        name=payload.admin_name,
        phone_number=payload.phone_number,
        email=payload.email,
        role="admin_empresa",
        company_id=new_company.id,
        firebase_uid=payload.firebase_uid
    )
    db.add(new_user)
    db.commit()
    return {"detail": "Empresa y administrador creados exitosamente", "company_code": new_company.company_code}

@app.post("/auth/seller/independent")
def register_independent(payload: SellerIndependent, db: Session = Depends(get_db)):
    existing_user_by_phone = db.query(User).filter(User.phone_number == payload.phone_number).first()
    existing_user_by_email = db.query(User).filter(User.email == payload.email).first()

    # Si el usuario ya existe por teléfono, actualizamos su correo y UID (para que pueda entrar con su cuenta de Google)
    if existing_user_by_phone:
        existing_user_by_phone.email = payload.email
        existing_user_by_phone.name = payload.name
        existing_user_by_phone.firebase_uid = payload.firebase_uid
        if payload.google_refresh_token:
            existing_user_by_phone.set_refresh_token(payload.google_refresh_token)
        db.commit()
        return {"detail": "Cuenta vinculada exitosamente al nuevo correo", "phone_number": existing_user_by_phone.phone_number}

    if existing_user_by_email:
         raise HTTPException(status_code=400, detail="Ya existe un vendedor con este correo pero diferente teléfono.")
         
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

class CalendarUpdate(BaseModel):
    calendar_id: str

@app.get("/api/calendars")
def get_calendars(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    refresh_token = user.get_refresh_token()
    if not refresh_token:
        raise HTTPException(status_code=400, detail="Google no conectado")
    
    try:
        calendars = GoogleCalendarService.list_calendars(refresh_token)
        return {"calendars": calendars, "selected": user.calendar_id}
    except Exception as e:
        logger.error(f"Error listing calendars: {e}")
        raise HTTPException(status_code=500, detail="Error de Google Calendar")

@app.post("/api/settings/calendar")
def update_calendar(email: str, payload: CalendarUpdate, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    user.calendar_id = payload.calendar_id
    db.commit()
    return {"detail": "Calendario actualizado", "calendar_id": user.calendar_id}

class WebhookPayload(BaseModel):
    phone: str
    message: str

@app.post("/api/whatsapp/webhook/asistto")
def asistto_webhook(payload: WebhookPayload, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Receives forwarded messages from Asistto. 
    Asistto must be configured to send a POST request with JSON: {"phone": "+52...", "message": "..."}
    """
    user = db.query(User).filter(User.phone_number == payload.phone).first()
    if not user:
        # Ignore messages from numbers not registered as salespeople
        return {"status": "ignored", "reason": "Not a registered salesperson"}
    
    if not user.company:
        return {"status": "error", "reason": "User has no company assigned"}
    
    # Get WhatsApp credentials from Company
    wa_token = user.company.get_whatsapp_token()
    wa_phone_id = user.company.whatsapp_phone_number_id
    
    if not wa_token or not wa_phone_id:
        logger.warning(f"Company {user.company.name} has no WhatsApp credentials configured.")
        return {"status": "error", "reason": "No WhatsApp credentials configured"}

    # We process the message in the background to return a fast 200 OK to the Webhook provider
    background_tasks.add_task(
        process_incoming_whatsapp_message, 
        user.id, 
        payload.phone, 
        payload.message, 
        wa_token, 
        wa_phone_id
    )

    return {"status": "accepted"}

def process_incoming_whatsapp_message(user_id: int, phone: str, message: str, wa_token: str, wa_phone_id: str):
    # Need a fresh session for the background task
    from database.connection import SessionLocal
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return

        try:
            refresh_token = user.get_refresh_token()
        except Exception:
            refresh_token = ""

        chat_history = redis_memory.get_history(phone)
        
        agent = GeminiAgent(
            user_refresh_token=refresh_token or "",
            spreadsheet_id=user.spreadsheet_id,
            template_doc_id=user.template_doc_id,
            sales_goals=user.sales_goals,
            objectives=user.objectives,
            calendar_id=user.calendar_id
        )

        reply, updated_history = agent.run(chat_history, message)

        redis_memory.add_message(phone, "user", message)
        redis_memory.add_message(phone, "agent", reply)

        db.add(ConversationLog(phone_number=phone, sender="user", message=message))
        db.add(ConversationLog(phone_number=phone, sender="agent", message=reply))
        db.commit()

        # Send the reply back via Meta Cloud API using the company's credentials
        from services.whatsapp_service import WhatsAppService
        WhatsAppService.send_text_message(phone, reply, token=wa_token, phone_id=wa_phone_id)

    except Exception as e:
        logger.error(f"Error processing background WhatsApp message: {e}")
    finally:
        db.close()


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
        objectives=user.objectives,
        calendar_id=user.calendar_id
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

@app.get("/seller/{email}")
def get_seller_profile(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    return {
        "id": user.id,
        "name": user.name,
        "phone_number": user.phone_number,
        "role": user.role,
        "sales_goals": user.sales_goals,
        "is_google_connected": user.encrypted_refresh_token is not None and user.encrypted_refresh_token != "",
        "company_code": user.company.company_code if user.company else None
    }

@app.put("/seller/{email}")
def update_seller_profile(email: str, payload: SellerUpdate, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    if payload.name is not None:
        user.name = payload.name
    if payload.phone_number is not None:
        user.phone_number = payload.phone_number
    if payload.sales_goals is not None:
        user.sales_goals = payload.sales_goals
        
    db.commit()
    return {"detail": "Perfil actualizado exitosamente"}

@app.get("/auth/google/url")
def get_google_auth_url(email: str, request: Request):
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/auth/google/callback"
    if "run.app" in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://")
    
    flow = Flow.from_client_config(
        get_client_secrets(),
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent',
        state=email
    )
    
    # Almacenar el code_verifier si PKCE está habilitado en la librería
    cv = getattr(flow, "code_verifier", None)
    if cv:
        redis_memory.set_state(f"oauth_{email}", "verifying", metadata={"code_verifier": cv}, ttl=600)
        
    return {"url": authorization_url}

@app.get("/auth/google/callback")
def google_auth_callback(state: str, code: str, request: Request, db: Session = Depends(get_db)):
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/auth/google/callback"
    if "run.app" in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://")

    user = db.query(User).filter(User.email == state).first()
    if not user:
        return HTMLResponse("<h1>Error</h1><p>Usuario no encontrado.</p>")

    flow = Flow.from_client_config(
        get_client_secrets(),
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    
    # Recuperar el code_verifier de Redis
    _, meta = redis_memory.get_state(f"oauth_{state}")
    code_verifier = meta.get("code_verifier")
    if code_verifier:
        flow.code_verifier = code_verifier
    
    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        if creds.refresh_token:
            user.set_refresh_token(creds.refresh_token)
            db.commit()
            return HTMLResponse("<div style='text-align:center; padding: 50px; font-family: sans-serif;'><h1 style='color:green;'>¡Google Workspace Conectado!</h1><p>Ya puedes cerrar esta ventana y regresar a tu panel.</p><script>setTimeout(()=>window.close(), 3000);</script></div>")
        else:
            return HTMLResponse("<div style='text-align:center; padding: 50px; font-family: sans-serif;'><h1 style='color:orange;'>Atención</h1><p>No se recibió un permiso permanente (refresh_token). Ve a tu cuenta de Google, remueve el acceso a la app, e inténtalo de nuevo aprobando todos los permisos.</p></div>")
    except Exception as e:
        logger.error(f"Error en OAuth Callback: {e}")
        return HTMLResponse(f"<div style='text-align:center; padding: 50px; font-family: sans-serif;'><h1 style='color:red;'>Error al vincular:</h1><p>{str(e)}</p></div>")

@app.get("/", response_class=HTMLResponse)
def read_root():
    import os
    file_path = os.path.join(os.path.dirname(__file__), "frontend.html")
    with open(file_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())
