from fastapi import FastAPI, Depends, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, EmailStr
from typing import Optional
import uuid
import logging
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth
from google_auth_oauthlib.flow import Flow

from database.connection import Base, engine, get_db
from database.models import User, ConversationLog, Company, AccountabilityPlan, DailyActivityLog, CalendarEventAudit, SlightEdgePlan, SlightEdgeLog
from agent.redis_memory import redis_memory
from agent.gemini_agent import GeminiAgent
from routers import whatsapp, cron, audit, slight_edge
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
                "ALTER TABLE users ADD COLUMN photo_url VARCHAR;",
                "ALTER TABLE companies ADD COLUMN whatsapp_phone_number_id VARCHAR;",
                "ALTER TABLE companies ADD COLUMN encrypted_whatsapp_token VARCHAR;",
                "ALTER TABLE companies ADD COLUMN global_goals TEXT;",
                "ALTER TABLE companies ADD COLUMN global_sales_target FLOAT DEFAULT 0.0;"
            ]
            for query in migrations:
                try:
                    conn.execute(text(query))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    logger.info(f"Migration skipped (likely exists): {e}")
            
            # Reset mangoex@gmail.com role to independent
            try:
                conn.execute(text("UPDATE users SET role = 'vendedor_independiente', company_id = NULL WHERE email = 'mangoex@gmail.com';"))
                conn.commit()
                logger.info("Successfully reset mangoex@gmail.com role to vendedor_independiente")
            except Exception as e:
                conn.rollback()
                logger.error(f"Failed to reset mangoex@gmail.com role: {e}")

        logger.info("Database tables initialized successfully.")
    except Exception as e:
        logger.critical(f"Failed to initialize database tables: {str(e)}")

# Include Routers
app.include_router(whatsapp.router)
app.include_router(cron.router)
app.include_router(audit.router)
app.include_router(slight_edge.router)

# Pydantic schemas
class CompanyCreate(BaseModel):
    name: str

class CompanyUpdate(BaseModel):
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_token: Optional[str] = None

class GlobalGoalsUpdate(BaseModel):
    global_goals: str
    global_sales_target: Optional[float] = 0.0

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
    objectives: Optional[str] = None
    photo_url: Optional[str] = None

class AIGoalsCalculationRequest(BaseModel):
    product_service: str
    ticket_average: float
    target_income: float
    custom_goal: Optional[str] = None

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
    if payload.global_sales_target is not None:
        company.global_sales_target = payload.global_sales_target
    db.commit()
    return {
        "status": "ok", 
        "global_goals": company.global_goals,
        "global_sales_target": company.global_sales_target
    }

def categorize_activity(name: str) -> str:
    n = name.lower().strip()
    if any(x in n for x in ["llam", "call", "prospect"]):
        return "llamada"
    if any(x in n for x in ["cit", "reun", "meet"]):
        return "cita"
    if any(x in n for x in ["cotiz", "propuest", "presupuest", "quot"]):
        return "cotizacion"
    if any(x in n for x in ["cierr", "vent", "cobro", "clos"]):
        return "venta"
    return "otra"

@app.get("/companies/{company_code}/dashboard")
def get_dashboard_metrics(company_code: str, db: Session = Depends(get_db)):
    from datetime import date, timedelta
    company = db.query(Company).filter(Company.company_code == company_code).first()
    if not company:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    
    sellers = db.query(User).filter(User.company_id == company.id).all()
    
    metrics_list = []
    total_sales = 0.0
    total_target = 0.0
    
    start_date = date.today() - timedelta(days=30)
    
    for s in sellers:
        # Get SlightEdgePlan
        plan = db.query(SlightEdgePlan).filter(SlightEdgePlan.user_id == s.id).first()
        target = plan.monthly_income_goal if plan else 0.0
        ticket = plan.ticket_average if plan else 0.0
        planned_conv_rate = plan.conversion_rate if plan else 0.0
        
        # Get logs for the last 30 days
        logs = db.query(SlightEdgeLog).filter(
            SlightEdgeLog.user_id == s.id,
            SlightEdgeLog.date >= start_date
        ).all()
        
        completed_calls = 0
        completed_meetings = 0
        completed_quotes = 0
        completed_sales = 0
        total_points = 0
        
        for log in logs:
            total_points += log.total_points or 0
            comp = log.completed_activities or {}
            for act_name, count in comp.items():
                cat = categorize_activity(act_name)
                val = max(0, count)
                if cat == "llamada":
                    completed_calls += val
                elif cat == "cita":
                    completed_meetings += val
                elif cat == "cotizacion":
                    completed_quotes += val
                elif cat == "venta":
                    completed_sales += val
        
        actual_sales_amount = completed_sales * ticket
        avg_daily_points = round(total_points / len(logs), 1) if logs else 0.0
        
        if completed_meetings > 0:
            actual_conv_rate = round((completed_sales / completed_meetings) * 100, 1)
        else:
            actual_conv_rate = planned_conv_rate
            
        metrics_list.append({
            "id": s.id,
            "name": s.name,
            "role": s.role,
            "sales_goals": s.sales_goals or f"Meta: ${target:,.2f}",
            "metrics": {
                "sales": actual_sales_amount,
                "target": target,
                "conversion_rate": actual_conv_rate,
                "roi": avg_daily_points, # mapping consistency points to roi for UI compatibility
                "clients": completed_calls # mapping calls to clients
            },
            "slight_edge": {
                "monthly_income_goal": target,
                "ticket_average": ticket,
                "planned_conversion_rate": planned_conv_rate,
                "actual_sales": actual_sales_amount,
                "avg_daily_points": avg_daily_points,
                "completed_calls": completed_calls,
                "completed_meetings": completed_meetings,
                "completed_quotes": completed_quotes,
                "completed_sales": completed_sales,
                "logged_days": len(logs)
            }
        })
        
        total_sales += actual_sales_amount
        total_target += target
        
    return {
        "global_goals": company.global_goals,
        "global_sales_target": company.global_sales_target or 0.0,
        "company_name": company.name,
        "aggregated": {
            "total_sales": total_sales,
            "total_target": total_target,
            "avg_conversion": round(sum([m["metrics"]["conversion_rate"] for m in metrics_list]) / len(metrics_list), 1) if metrics_list else 0.0,
            "avg_roi": round(sum([m["metrics"]["roi"] for m in metrics_list]) / len(metrics_list), 1) if metrics_list else 0.0
        },
        "sellers": metrics_list
    }

@app.post("/companies/{company_code}/sellers/{seller_id}/ai-goals")
def suggest_ai_goals(company_code: str, seller_id: int, payload: AIGoalProposalRequest, db: Session = Depends(get_db)):
    from datetime import date, timedelta
    company = db.query(Company).filter(Company.company_code == company_code).first()
    if not company:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
        
    seller = db.query(User).filter(User.id == seller_id, User.company_id == company.id).first()
    if not seller:
        raise HTTPException(status_code=404, detail="Vendedor no encontrado en esta empresa")
        
    try:
        # Get SlightEdgePlan
        plan = db.query(SlightEdgePlan).filter(SlightEdgePlan.user_id == seller.id).first()
        target = plan.monthly_income_goal if plan else 0.0
        ticket = plan.ticket_average if plan else 0.0
        planned_conv_rate = plan.conversion_rate if plan else 0.0
        daily_points_goal = plan.daily_points_goal if plan else 10
        activities_config = plan.activities_config if plan else []
        
        # Get logs for the last 30 days
        start_date = date.today() - timedelta(days=30)
        logs = db.query(SlightEdgeLog).filter(
            SlightEdgeLog.user_id == seller.id,
            SlightEdgeLog.date >= start_date
        ).all()
        
        completed_calls = 0
        completed_meetings = 0
        completed_quotes = 0
        completed_sales = 0
        total_points = 0
        
        for log in logs:
            total_points += log.total_points or 0
            comp = log.completed_activities or {}
            for act_name, count in comp.items():
                cat = categorize_activity(act_name)
                val = max(0, count)
                if cat == "llamada":
                    completed_calls += val
                elif cat == "cita":
                    completed_meetings += val
                elif cat == "cotizacion":
                    completed_quotes += val
                elif cat == "venta":
                    completed_sales += val
        
        actual_sales_amount = completed_sales * ticket
        avg_daily_points = round(total_points / len(logs), 1) if logs else 0.0
        
        from agent.gemini_agent import GenerativeModel
        model = GenerativeModel(model_name="gemini-2.5-pro")
        
        prompt = f"""
        Eres un coach de ventas experto especializado en la metodología "La Ligera Ventaja" (The Slight Edge) de Jeff Olson.
        La empresa '{company.name}' tiene las siguientes directrices y metas:
        - Meta de Facturación Mensual Global de la Empresa: ${company.global_sales_target:,.2f}
        - Estrategia Global: {company.global_goals or 'Maximizar ingresos y consistencia.'}
        
        Analiza al vendedor '{seller.name}' con base en su plan de La Ventaja y su desempeño de los últimos 30 días:
        
        PLAN ACTUAL DE LA VENTAJA:
        - Meta de ingresos mensuales del vendedor: ${target:,.2f}
        - Ticket promedio: ${ticket:,.2f}
        - Tasa de conversión planificada: {planned_conv_rate}%
        - Meta diaria de puntos: {daily_points_goal} pts
        - Disciplinas diarias configuradas en su plan: {activities_config}
        
        RENDIMIENTO REAL EN LOS ÚLTIMOS 30 DÍAS:
        - Llamadas completadas: {completed_calls}
        - Citas completadas: {completed_meetings}
        - Cotizaciones completadas: {completed_quotes}
        - Ventas reales logradas (cierres): {completed_sales} (Monto estimado: ${actual_sales_amount:,.2f})
        - Consistencia promedio diaria: {avg_daily_points} pts (Meta diaria: {daily_points_goal} pts)
        - Días con registro de actividades: {len(logs)} días
        
        Genera una sugerencia de coaching concisa y accionable para este vendedor (máximo 3 párrafos cortos, en español).
        Compara su desempeño real contra sus metas planificadas y sugiere si debe ajustar sus disciplinas, mejorar su consistencia diaria, o si sus metas están correctamente alineadas para lograr el éxito global de la empresa.
        No uses saludos ni introducciones, responde directamente con la recomendación.
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
    if payload.objectives is not None:
        seller.objectives = payload.objectives
    
    db.commit()
    return {"status": "ok", "sales_goals": seller.sales_goals, "objectives": seller.objectives}

@app.post("/seller/goals/calculate-ai")
def calculate_seller_goals_ai(payload: AIGoalsCalculationRequest):
    import json
    try:
        from agent.gemini_agent import GenerativeModel
        model = GenerativeModel(model_name="gemini-2.5-pro")
        
        prompt = f"""
        Eres un Sales Coach experto en planificación financiera y estratégica para vendedores independientes.
        Ayuda al vendedor a calcular y establecer sus metas numéricas y directrices semanales basándose en:
        - Producto/Servicio que vende: {payload.product_service}
        - Precio promedio de venta (Ticket Promedio): ${payload.ticket_average:,.2f} MXN
        - Ingreso mensual neto deseado: ${payload.target_income:,.2f} MXN
        {f'- Meta manual / notas adicionales: {payload.custom_goal}' if payload.custom_goal else ''}
        
        Realiza lo siguiente:
        1. Calcula cuántas ventas concretadas al mes necesita realizar para lograr su ingreso mensual deseado (asumiendo un margen razonable si es servicio, o dividiendo el ingreso entre el ticket promedio, explícalo de forma muy concisa).
        2. Estima cuántos prospectos/contactos necesita iniciar al mes asumiendo una tasa de conversión estándar de la industria (ej. 10% a 20%) para lograr esas ventas.
        3. Genera un plan de acción semanal corto (ej. cuántas citas agendar en su calendario, cuántas llamadas hacer, etc.).
        4. Redacta dos secciones claras que encajen en este JSON:
           - "sales_goals": Las metas numéricas concretas (ej. "Vender $150,000 mensuales, concretando 10 ventas de $15,000").
           - "objectives": El enfoque estratégico y actividades semanales del Coach de IA (ej. "Llamar a 5 prospectos al día, agendar 3 demostraciones por semana, y hacer seguimiento a cotizaciones los viernes").
        
        Responde ÚNICAMENTE con un objeto JSON válido con las llaves "sales_goals" y "objectives". No uses Markdown, no uses bloques de código (```json), responde texto plano JSON.
        """
        
        response = model.generate_content(prompt)
        text_response = response.text.strip()
        # Clean markdown code blocks if any
        if text_response.startswith("```json"):
            text_response = text_response[7:]
        if text_response.endswith("```"):
            text_response = text_response[:-3]
        text_response = text_response.strip()
        
        data = json.loads(text_response)
        return data
    except Exception as e:
        logger.error(f"Error calculating goals: {e}")
        # Return fallback values
        return {
            "sales_goals": f"Meta sugerida: Vender ${(payload.target_income * 2):,.2f} MXN al mes.",
            "objectives": f"Enfoque sugerido:\n1. Vender {payload.product_service}.\n2. Realizar llamadas de prospección semanal.\n3. Agendar citas en Google Calendar."
        }


class AccountabilityPlanUpdate(BaseModel):
    citas_meta_mensual: int
    citas_meta_semanal: int
    citas_meta_diaria: int
    llamadas_meta_mensual: int
    llamadas_meta_semanal: int
    llamadas_meta_diaria: int
    propuestas_meta_mensual: int
    propuestas_meta_semanal: int
    propuestas_meta_diaria: int


class DailyActivityLogUpdate(BaseModel):
    date_str: str  # YYYY-MM-DD
    citas: int
    llamadas: int
    propuestas: int


class EventCompletionRequest(BaseModel):
    is_completed: bool
    summary: Optional[str] = None
    start_time: Optional[str] = None


@app.get("/seller/{user_id}/accountability/plan")
def get_accountability_plan(user_id: int, db: Session = Depends(get_db)):
    plan = db.query(AccountabilityPlan).filter(AccountabilityPlan.user_id == user_id).first()
    if not plan:
        # Create a default plan with all zeros
        plan = AccountabilityPlan(
            user_id=user_id,
            citas_meta_mensual=0, citas_meta_semanal=0, citas_meta_diaria=0,
            llamadas_meta_mensual=0, llamadas_meta_semanal=0, llamadas_meta_diaria=0,
            propuestas_meta_mensual=0, propuestas_meta_semanal=0, propuestas_meta_diaria=0
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)
    return plan


@app.put("/seller/{user_id}/accountability/plan")
def update_accountability_plan(user_id: int, payload: AccountabilityPlanUpdate, db: Session = Depends(get_db)):
    plan = db.query(AccountabilityPlan).filter(AccountabilityPlan.user_id == user_id).first()
    if not plan:
        plan = AccountabilityPlan(user_id=user_id)
        db.add(plan)
    
    plan.citas_meta_mensual = payload.citas_meta_mensual
    plan.citas_meta_semanal = payload.citas_meta_semanal
    plan.citas_meta_diaria = payload.citas_meta_diaria
    
    plan.llamadas_meta_mensual = payload.llamadas_meta_mensual
    plan.llamadas_meta_semanal = payload.llamadas_meta_semanal
    plan.llamadas_meta_diaria = payload.llamadas_meta_diaria
    
    plan.propuestas_meta_mensual = payload.propuestas_meta_mensual
    plan.propuestas_meta_semanal = payload.propuestas_meta_semanal
    plan.propuestas_meta_diaria = payload.propuestas_meta_diaria
    
    db.commit()
    db.refresh(plan)
    return plan


@app.get("/seller/{user_id}/accountability/logs")
def get_accountability_logs(user_id: int, db: Session = Depends(get_db)):
    import pytz
    from datetime import datetime, timedelta
    
    seller = db.query(User).filter(User.id == user_id).first()
    if not seller:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
        
    cal_tz = "America/Mexico_City"
    try:
        refresh_token = seller.get_refresh_token()
        if refresh_token:
            metadata = GoogleCalendarService.get_calendar_metadata(refresh_token, seller.calendar_id)
            cal_tz = metadata.get("timeZone", "America/Mexico_City")
    except Exception:
        pass
        
    tz = pytz.timezone(cal_tz)
    now_local = datetime.now(tz)
    today = now_local.date()
    
    # Calculate boundaries
    start_month = today.replace(day=1)
    start_week = today - timedelta(days=today.weekday())
    
    # Query logs from start of month
    logs = db.query(DailyActivityLog).filter(
        DailyActivityLog.user_id == user_id,
        DailyActivityLog.date >= start_month
    ).all()
    
    # Query completed calendar audits
    audits = db.query(CalendarEventAudit).filter(
        CalendarEventAudit.user_id == user_id,
        CalendarEventAudit.is_completed == True
    ).all()
    
    # Count completed events by local date
    completed_events_by_date = {}
    for audit in audits:
        dt_to_use = audit.event_start or audit.created_at
        if dt_to_use:
            dt_local = dt_to_use.astimezone(tz) if dt_to_use.tzinfo else tz.localize(dt_to_use)
            d = dt_local.date()
            if d >= start_month:
                completed_events_by_date[d] = completed_events_by_date.get(d, 0) + 1
                
    # Gather all dates to process
    all_dates = set()
    log_map = {}
    for log in logs:
        all_dates.add(log.date)
        log_map[log.date] = log
        
    for d in completed_events_by_date.keys():
        all_dates.add(d)
    
    # Sum up actuals
    today_log = {"citas": 0, "llamadas": 0, "propuestas": 0}
    weekly = {"citas": 0, "llamadas": 0, "propuestas": 0}
    monthly = {"citas": 0, "llamadas": 0, "propuestas": 0}
    
    for d in all_dates:
        log = log_map.get(d)
        citas_log = log.citas_completadas if (log and log.citas_completadas is not None) else 0
        llamadas_comp = log.llamadas_completadas if (log and log.llamadas_completadas is not None) else 0
        propuestas_comp = log.propuestas_completadas if (log and log.propuestas_completadas is not None) else 0
        
        # Max of logged value and calendar audit count
        citas_comp = max(citas_log, completed_events_by_date.get(d, 0))
        
        # Check today
        if d == today:
            today_log["citas"] = citas_comp
            today_log["llamadas"] = llamadas_comp
            today_log["propuestas"] = propuestas_comp
        
        # Check current week
        if d >= start_week:
            weekly["citas"] += citas_comp
            weekly["llamadas"] += llamadas_comp
            weekly["propuestas"] += propuestas_comp
            
        # Monthly is all logs since start_month
        monthly["citas"] += citas_comp
        monthly["llamadas"] += llamadas_comp
        monthly["propuestas"] += propuestas_comp
        
    # Get plan
    plan = db.query(AccountabilityPlan).filter(AccountabilityPlan.user_id == user_id).first()
    plan_data = {
        "daily": {"citas": 0, "llamadas": 0, "propuestas": 0},
        "weekly": {"citas": 0, "llamadas": 0, "propuestas": 0},
        "monthly": {"citas": 0, "llamadas": 0, "propuestas": 0}
    }
    if plan:
        plan_data["daily"] = {"citas": plan.citas_meta_diaria, "llamadas": plan.llamadas_meta_diaria, "propuestas": plan.propuestas_meta_diaria}
        plan_data["weekly"] = {"citas": plan.citas_meta_semanal, "llamadas": plan.llamadas_meta_semanal, "propuestas": plan.propuestas_meta_semanal}
        plan_data["monthly"] = {"citas": plan.citas_meta_mensual, "llamadas": plan.llamadas_meta_mensual, "propuestas": plan.propuestas_meta_mensual}
        
    return {
        "today": today_log,
        "weekly": weekly,
        "monthly": monthly,
        "plan": plan_data,
        "date": today.isoformat()
    }


@app.post("/seller/{user_id}/accountability/logs")
def update_daily_activity_log(user_id: int, payload: DailyActivityLogUpdate, db: Session = Depends(get_db)):
    from datetime import datetime
    try:
        log_date = datetime.strptime(payload.date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de fecha inválido. Usar YYYY-MM-DD")
        
    log = db.query(DailyActivityLog).filter(
        DailyActivityLog.user_id == user_id,
        DailyActivityLog.date == log_date
    ).first()
    
    if not log:
        log = DailyActivityLog(user_id=user_id, date=log_date, citas_completadas=0, llamadas_completadas=0, propuestas_completadas=0)
        db.add(log)
        
    log.citas_completadas = payload.citas
    log.llamadas_completadas = payload.llamadas
    log.propuestas_completadas = payload.propuestas
    
    db.commit()
    db.refresh(log)
    return {
        "status": "ok",
        "date": log.date.isoformat(),
        "citas": log.citas_completadas,
        "llamadas": log.llamadas_completadas,
        "propuestas": log.propuestas_completadas
    }


@app.get("/seller/{user_id}/accountability/calendar-events")
def get_seller_calendar_events(user_id: int, db: Session = Depends(get_db)):
    seller = db.query(User).filter(User.id == user_id).first()
    if not seller:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
        
    try:
        refresh_token = seller.get_refresh_token()
    except Exception:
        raise HTTPException(status_code=400, detail="Google OAuth no configurado")
        
    import pytz
    from datetime import datetime
    cal_tz = "America/Mexico_City"
    try:
        metadata = GoogleCalendarService.get_calendar_metadata(refresh_token, seller.calendar_id)
        cal_tz = metadata.get("timeZone", "America/Mexico_City")
    except Exception:
        pass
        
    tz = pytz.timezone(cal_tz)
    now_local = datetime.now(tz)
    today_str = now_local.strftime("%Y-%m-%d")
    
    # List events
    try:
        events = GoogleCalendarService.list_events(refresh_token, today_str, seller.calendar_id)
    except Exception as e:
        logger.error(f"Error listing events for accountability: {e}")
        events = []
        
    # Get audit status from database for these events
    event_ids = [e["id"] for e in events if e.get("id")]
    audits = db.query(CalendarEventAudit).filter(
        CalendarEventAudit.user_id == user_id,
        CalendarEventAudit.event_id.in_(event_ids)
    ).all() if event_ids else []
    
    audit_map = {a.event_id: a for a in audits}
    
    enriched_events = []
    for e in events:
        eid = e.get("id")
        audit = audit_map.get(eid)
        
        enriched_events.append({
            "id": eid,
            "summary": e.get("summary", "Sin Título"),
            "start": e.get("start"),
            "end": e.get("end"),
            "is_completed": audit.is_completed if audit else False,
            "audit_status": audit.audit_status if audit else "pending"
        })
        
    return enriched_events


@app.post("/seller/{user_id}/accountability/calendar-events/{event_id}/complete")
def complete_calendar_event(user_id: int, event_id: str, payload: EventCompletionRequest, db: Session = Depends(get_db)):
    import pytz
    from datetime import datetime
    
    audit = db.query(CalendarEventAudit).filter(
        CalendarEventAudit.user_id == user_id,
        CalendarEventAudit.event_id == event_id
    ).first()
    
    was_completed = audit.is_completed if audit else False
    
    if not audit:
        # Determine start date
        event_start_dt = None
        if payload.start_time:
            try:
                event_start_dt = datetime.fromisoformat(payload.start_time.replace("Z", "+00:00"))
            except ValueError:
                pass
        
        audit = CalendarEventAudit(
            user_id=user_id,
            event_id=event_id,
            event_summary=payload.summary or "Cita",
            event_start=event_start_dt,
            is_completed=payload.is_completed,
            audit_status="confirmed" if payload.is_completed else "no_show"
        )
        db.add(audit)
    else:
        audit.is_completed = payload.is_completed
        audit.audit_status = "confirmed" if payload.is_completed else "no_show"
        
    db.commit()
    db.refresh(audit)
    
    # If completion status changed to True, increment DailyActivityLog.citas_completadas for the event's date
    if payload.is_completed and not was_completed:
        # Get event's date in local timezone or current date
        seller = db.query(User).filter(User.id == user_id).first()
        cal_tz = "America/Mexico_City"
        try:
            refresh_token = seller.get_refresh_token()
            if refresh_token:
                metadata = GoogleCalendarService.get_calendar_metadata(refresh_token, seller.calendar_id)
                cal_tz = metadata.get("timeZone", "America/Mexico_City")
        except Exception:
            pass
            
        tz = pytz.timezone(cal_tz)
        if audit.event_start:
            # Convert event_start (naive or aware) to user local time
            dt_local = audit.event_start.astimezone(tz) if audit.event_start.tzinfo else tz.localize(audit.event_start)
            log_date = dt_local.date()
        else:
            log_date = datetime.now(tz).date()
            
        # Get log for this date
        log = db.query(DailyActivityLog).filter(
            DailyActivityLog.user_id == user_id,
            DailyActivityLog.date == log_date
        ).first()
        
        if not log:
            log = DailyActivityLog(user_id=user_id, date=log_date, citas_completadas=0, llamadas_completadas=0, propuestas_completadas=0)
            db.add(log)
        else:
            if log.citas_completadas is None: log.citas_completadas = 0
            if log.llamadas_completadas is None: log.llamadas_completadas = 0
            if log.propuestas_completadas is None: log.propuestas_completadas = 0
            
        log.citas_completadas += 1
        db.commit()
        
    return {"status": "ok", "event_id": event_id, "is_completed": audit.is_completed}


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
        events = GoogleCalendarService.get_upcoming_events(refresh_token, days_ahead=7, calendar_id=seller.calendar_id)
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
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="El correo o teléfono ya está en uso por otro vendedor.")
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
    
    # Resolve company credentials if any, otherwise fall back to global settings
    wa_token = user.company.get_whatsapp_token() if user.company else settings.WHATSAPP_TOKEN
    wa_phone_id = user.company.whatsapp_phone_number_id if user.company else settings.WHATSAPP_PHONE_NUMBER_ID
    
    if not wa_token or not wa_phone_id or wa_token == "EAAXxXX..." or wa_phone_id == "1234567890":
        logger.warning(f"No valid WhatsApp credentials for user {user.email}.")
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
            calendar_id=user.calendar_id,
            phone_number=phone
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
        calendar_id=user.calendar_id,
        phone_number=payload.phone_number
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
        "photo_url": user.photo_url,
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
    if payload.photo_url is not None:
        user.photo_url = payload.photo_url
        
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
