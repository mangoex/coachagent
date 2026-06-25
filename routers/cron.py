from fastapi import APIRouter, Depends, status, HTTPException, Request, BackgroundTasks
from sqlalchemy.orm import Session
import logging
from datetime import datetime
import json
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from database.connection import get_db, SessionLocal
from database.models import User, ConversationLog, Company, CalendarEventAudit, DailyActivityLog
from services.calendar_service import GoogleCalendarService
from services.sheets_service import GoogleSheetsService
from services.whatsapp_service import WhatsAppService
from agent.redis_memory import redis_memory
from agent.gemini_agent import GeminiAgent
from config.settings import settings

try:
    from google.cloud import tasks_v2
    from google.protobuf import timestamp_pb2
    CLOUD_TASKS_AVAILABLE = True
except ImportError:
    CLOUD_TASKS_AVAILABLE = False

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cron", tags=["cron"])

class WorkerPayload(BaseModel):
    user_id: int

def enqueue_cron_task(endpoint_path: str, user_id: int, background_tasks: BackgroundTasks):
    """
    Enqueues a cron task. Uses Google Cloud Tasks if available and in production,
    otherwise falls back to FastAPI BackgroundTasks.
    """
    payload = {"user_id": user_id}
    
    # Check if Cloud Tasks is configured and we're not in local dev
    if CLOUD_TASKS_AVAILABLE and settings.ENVIRONMENT != "development" and settings.CLOUD_TASKS_QUEUE != "agent-queue":
        try:
            client = tasks_v2.CloudTasksClient()
            parent = client.queue_path(settings.GCP_PROJECT_ID, settings.GCP_LOCATION, settings.CLOUD_TASKS_QUEUE)
            
            url = f"{settings.BASE_URL.rstrip('/')}{endpoint_path}"
            
            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": url,
                    "headers": {"Content-type": "application/json"},
                    "body": json.dumps(payload).encode()
                }
            }
            
            client.create_task(request={"parent": parent, "task": task})
            logger.info(f"Enqueued Cloud Task for {url} (user_id: {user_id})")
            return
        except Exception as e:
            logger.error(f"Failed to enqueue Cloud Task for user {user_id}, falling back to BackgroundTasks: {e}")
            
    # Fallback to local background tasks
    # We call the handler helper directly using a new DB session
    background_tasks.add_task(process_single_task_background, endpoint_path, user_id)

def process_single_task_background(endpoint_path: str, user_id: int):
    db = SessionLocal()
    try:
        if "morning-briefing" in endpoint_path:
            process_single_morning_briefing(user_id, db)
        elif "evening-accountability" in endpoint_path:
            process_single_evening_accountability(user_id, db)
        elif "daily-plan" in endpoint_path:
            process_single_daily_plan(user_id, db)
    except Exception as e:
        logger.error(f"Error in background task {endpoint_path} for user {user_id}: {e}")
    finally:
        db.close()


# --- Worker Handlers ---

def process_single_morning_briefing(user_id: int, db: Session):
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.phone_number:
        return

    logger.info(f"Generating morning briefing for user {user.email}...")
    try:
        refresh_token = user.get_refresh_token()
    except Exception as e:
        logger.error(f"Failed to decrypt token for morning briefing of {user.email}: {str(e)}")
        return

    # 1. Fetch today's agenda
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        events = GoogleCalendarService.list_events(refresh_token, today_str)
    except Exception as e:
        logger.error(f"Error fetching calendar for morning briefing of {user.email}: {str(e)}")
        events = []

    # 2. Fetch CRM quota stats from Sheets
    goal_progress = "65%"
    if user.spreadsheet_id:
        try:
            crm_data = GoogleSheetsService.read_crm_data(refresh_token, user.spreadsheet_id)
            if crm_data.get("clientes"):
                total_clients = len(crm_data["clientes"])
                goal_progress = f"{min(35 + total_clients * 10, 95)}%"
        except Exception as e:
            logger.error(f"Error reading CRM for morning briefing of {user.email}: {str(e)}")

    # 3. Generate summary briefing using Gemini
    events_desc = ""
    if events:
        events_desc = "\n".join([f"- {e['summary']} at {e['start']}" for e in events])
    else:
        events_desc = "No meetings scheduled for today."

    prompt = (
        f"Por favor, redacta un mensaje de WhatsApp para el vendedor {user.name}.\n"
        f"Hoy es {today_str}.\n"
        f"Agenda de hoy:\n{events_desc}\n"
        f"Progreso de cuota semanal de ventas: {goal_progress}.\n\n"
        "Instrucciones: El mensaje debe ser proactivo, animado, muy ejecutivo y enfocado al cierre de ventas. "
        "Enumera las reuniones importantes de hoy y dale aliento para alcanzar el objetivo semanal. "
        "Sé breve y no uses placeholders (e.g. [Nombre]). Escribe en español."
    )

    agent = GeminiAgent(
        user_refresh_token=refresh_token,
        spreadsheet_id=user.spreadsheet_id,
        template_doc_id=user.template_doc_id,
        phone_number=user.phone_number
    )

    briefing_text, _ = agent.run([], prompt)

    # Get company credentials if the user belongs to one
    company_token = None
    phone_id = None
    if user.company_id:
        company = db.query(Company).filter(Company.id == user.company_id).first()
        if company:
            company_token = company.get_whatsapp_token()
            phone_id = company.whatsapp_phone_number_id

    # 4. Send briefing via WhatsApp
    components = [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": briefing_text}
            ]
        }
    ]
    
    success = WhatsAppService.send_template_message(
        to_phone=user.phone_number, 
        template_name="morning_briefing",
        components=components,
        token=company_token,
        phone_id=phone_id
    )

    # Log agent response in DB
    db_log = ConversationLog(
        phone_number=user.phone_number, 
        sender="agent", 
        message=briefing_text,
        user_id=user.id,
        company_id=user.company_id
    )
    db.add(db_log)
    db.commit()
    logger.info(f"Morning briefing sent to {user.email} (success={success})")


def process_single_evening_accountability(user_id: int, db: Session):
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.phone_number:
        return

    logger.info(f"Running evening accountability check for {user.email}...")
    try:
        refresh_token = user.get_refresh_token()
        if not refresh_token:
            return
    except Exception as e:
        logger.error(f"Failed to decrypt token for accountability check of {user.email}: {str(e)}")
        return

    # Fetch timezone and today's date in user local time
    import pytz
    cal_tz = "America/Mexico_City"
    try:
        metadata = GoogleCalendarService.get_calendar_metadata(refresh_token, user.calendar_id)
        cal_tz = metadata.get("timeZone", "America/Mexico_City")
    except Exception:
        pass
    tz = pytz.timezone(cal_tz)
    now_local = datetime.now(tz)
    today_str = now_local.strftime("%Y-%m-%d")

    # Fetch today's agenda to find meetings
    try:
        events = GoogleCalendarService.list_events(refresh_token, today_str, user.calendar_id)
    except Exception as e:
        logger.error(f"Error fetching calendar for accountability of {user.email}: {str(e)}")
        events = []

    # Get company credentials if the user belongs to one
    company_token = user.company.get_whatsapp_token() if user.company else None
    phone_id = user.company.whatsapp_phone_number_id if user.company else None

    if not events:
        msg = f"¡Hola {user.name}! He revisado tu agenda y no tenías reuniones hoy. ¡Espero que hayas tenido un día productivo!"
        WhatsAppService.send_text_message(user.phone_number, msg, token=company_token, phone_id=phone_id)
        
        db_log = ConversationLog(
            phone_number=user.phone_number, 
            sender="agent", 
            message=msg,
            user_id=user.id,
            company_id=user.company_id
        )
        db.add(db_log)
        db.commit()
        return

    # Find un-audited events today
    event_ids = [e["id"] for e in events if e.get("id")]
    audits = db.query(CalendarEventAudit).filter(
        CalendarEventAudit.user_id == user.id,
        CalendarEventAudit.event_id.in_(event_ids)
    ).all() if event_ids else []
    audited_map = {a.event_id: a for a in audits}
    
    un_audited_events = []
    for e in events:
        eid = e.get("id")
        audit = audited_map.get(eid)
        if not audit or audit.audit_status == "pending":
            un_audited_events.append(e)

    if not un_audited_events:
        return

    # Find the first un-audited meeting to audit
    audit_meeting = un_audited_events[0]
    eid = audit_meeting["id"]
    meeting_summary = audit_meeting["summary"]
    
    audit_entry = db.query(CalendarEventAudit).filter(
        CalendarEventAudit.user_id == user.id,
        CalendarEventAudit.event_id == eid
    ).first()
    
    if not audit_entry:
        start_dt = None
        start_str = audit_meeting.get("start")
        if start_str:
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except Exception:
                pass
        audit_entry = CalendarEventAudit(
            user_id=user.id,
            event_id=eid,
            event_summary=meeting_summary,
            event_start=start_dt,
            is_completed=False,
            audit_status="sent_whatsapp"
        )
        db.add(audit_entry)
    else:
        audit_entry.audit_status = "sent_whatsapp"
    db.commit()

    # Save state machine in Redis
    redis_memory.set_state(
        phone_number=user.phone_number,
        state="AWAITING_MEETING_FEEDBACK",
        metadata={
            "meeting_id": eid,
            "meeting_summary": meeting_summary
        },
        ttl=14400
    )

    body_text = f"Hola {user.name}, ¿cómo te fue en tu junta de hoy: '{meeting_summary}'? ¿Se realizó la reunión?"
    
    buttons = [
        {"id": f"btn_yes_{eid}", "title": "Sí, se realizó"},
        {"id": f"btn_no_{eid}", "title": "No se realizó"}
    ]
    
    success = WhatsAppService.send_interactive_buttons(
        to_phone=user.phone_number,
        body_text=body_text,
        buttons=buttons,
        token=company_token,
        phone_id=phone_id
    )
    
    if not success:
        components = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": body_text}
                ]
            }
        ]
        success = WhatsAppService.send_template_message(
            to_phone=user.phone_number,
            template_name="evening_accountability",
            components=components,
            token=company_token,
            phone_id=phone_id
        )

    # Log agent message to DB
    db_log = ConversationLog(
        phone_number=user.phone_number, 
        sender="agent", 
        message=body_text,
        user_id=user.id,
        company_id=user.company_id
    )
    db.add(db_log)
    db.commit()


def process_single_daily_plan(user_id: int, db: Session):
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.phone_number:
        return

    logger.info(f"Generating daily plan for user {user.email}...")
    try:
        refresh_token = user.get_refresh_token()
    except Exception as e:
        logger.error(f"Failed to decrypt token for daily plan of {user.email}: {str(e)}")
        return

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    prompt = (
        f"Hola, eres el Sales Coach AI de {user.name}.\n"
        f"Hoy es {today_str}. Las metas de venta de {user.name} son las siguientes:\n"
        f"{user.sales_goals}\n\n"
        "Por favor, redacta un plan de acción sugerido para HOY. Sé proactivo, motivador, y da 2 o 3 pasos claros "
        "o tareas que el vendedor debe realizar hoy para acercarse a estas metas. "
        "El mensaje no debe tener placeholders. El mensaje va directo al vendedor por WhatsApp."
    )

    agent = GeminiAgent(
        user_refresh_token=refresh_token,
        spreadsheet_id=user.spreadsheet_id,
        template_doc_id=user.template_doc_id,
        phone_number=user.phone_number
    )

    plan_text, _ = agent.run([], prompt)

    company_token = None
    phone_id = None
    if user.company_id:
        company = db.query(Company).filter(Company.id == user.company_id).first()
        if company:
            company_token = company.get_whatsapp_token()
            phone_id = company.whatsapp_phone_number_id

    components = [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": plan_text[:1024]}
            ]
        }
    ]
    
    success = WhatsAppService.send_template_message(
        to_phone=user.phone_number, 
        template_name="daily_plan_template",
        components=components,
        token=company_token,
        phone_id=phone_id
    )

    if success:
        db_log = ConversationLog(
            phone_number=user.phone_number, 
            sender="agent", 
            message=plan_text,
            user_id=user.id,
            company_id=user.company_id
        )
        db.add(db_log)
        db.commit()


# --- Endpoint Routes ---

@router.post("/morning-briefing")
def morning_briefing(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Scheduler trigger endpoint. Queries active users and enqueues their briefings.
    """
    users = db.query(User).filter(User.phone_number.isnot(None)).all()
    for user in users:
        enqueue_cron_task("/cron/morning-briefing/worker", user.id, background_tasks)
    return {"status": "enqueued", "processed_users_count": len(users)}

@router.post("/morning-briefing/worker")
def morning_briefing_worker(payload: WorkerPayload, db: Session = Depends(get_db)):
    process_single_morning_briefing(payload.user_id, db)
    return {"status": "completed", "user_id": payload.user_id}


@router.post("/evening-accountability")
def evening_accountability(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Scheduler trigger endpoint. Queries active users and enqueues accountability checks.
    """
    users = db.query(User).filter(User.phone_number.isnot(None)).all()
    for user in users:
        enqueue_cron_task("/cron/evening-accountability/worker", user.id, background_tasks)
    return {"status": "enqueued", "processed_users_count": len(users)}

@router.post("/evening-accountability/worker")
def evening_accountability_worker(payload: WorkerPayload, db: Session = Depends(get_db)):
    process_single_evening_accountability(payload.user_id, db)
    return {"status": "completed", "user_id": payload.user_id}


@router.post("/daily_plan")
def daily_plan(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Scheduler trigger endpoint. Queries active users with goals and enqueues daily plans.
    """
    users = db.query(User).filter(
        User.phone_number.isnot(None), 
        User.sales_goals.isnot(None), 
        User.sales_goals != ""
    ).all()
    for user in users:
        enqueue_cron_task("/cron/daily-plan/worker", user.id, background_tasks)
    return {"status": "enqueued", "processed_users_count": len(users)}

@router.post("/daily-plan/worker")
def daily_plan_worker(payload: WorkerPayload, db: Session = Depends(get_db)):
    process_single_daily_plan(payload.user_id, db)
    return {"status": "completed", "user_id": payload.user_id}


@router.post("/cloud-task-callback")
async def cloud_task_callback(request: Request, db: Session = Depends(get_db)):
    """
    Webhook target for Google Cloud Tasks for dynamic scheduled messages.
    """
    body = await request.json()
    phone_number = body.get("phone_number")
    message = body.get("message")
    
    if not phone_number or not message:
        return {"status": "error", "message": "Missing phone_number or message"}
        
    user = db.query(User).filter(User.phone_number == phone_number).first()
    if not user:
        return {"status": "error", "message": "User not found"}
        
    company_token = None
    phone_id = None
    if user.company_id:
        company = db.query(Company).filter(Company.id == user.company_id).first()
        if company:
            company_token = company.get_whatsapp_token()
            phone_id = company.whatsapp_phone_number_id

    success = WhatsAppService.send_text_message(
        to_phone=user.phone_number,
        text=message,
        token=company_token,
        phone_id=phone_id
    )
    
    if success:
        db_log = ConversationLog(
            phone_number=user.phone_number, 
            sender="agent", 
            message=message,
            user_id=user.id,
            company_id=user.company_id
        )
        db.add(db_log)
        db.commit()
        return {"status": "ok"}
    else:
        return {"status": "failed", "message": "Failed to send WhatsApp message"}
