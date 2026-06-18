from fastapi import APIRouter, Depends, status, HTTPException, Request
from sqlalchemy.orm import Session
import logging
from datetime import datetime
import json

from database.connection import get_db
from database.models import User, ConversationLog, Company
from services.calendar_service import GoogleCalendarService
from services.sheets_service import GoogleSheetsService
from services.whatsapp_service import WhatsAppService
from agent.redis_memory import redis_memory
from agent.gemini_agent import GeminiAgent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cron", tags=["cron"])

@router.post("/morning-briefing")
def morning_briefing(db: Session = Depends(get_db)):
    """
    Triggered by Cloud Scheduler at 8:00 AM.
    Fetches today's agenda, CRM stats, generates a motivational briefing with Gemini, and sends it via WhatsApp.
    """
    users = db.query(User).all()
    results = []

    for user in users:
        if not user.phone_number:
            continue
            
        logger.info(f"Generating morning briefing for user {user.email}...")
        
        try:
            refresh_token = user.get_refresh_token()
        except Exception as e:
            logger.error(f"Failed to decrypt token for morning briefing of {user.email}: {str(e)}")
            continue

        # 1. Fetch today's agenda
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            events = GoogleCalendarService.list_events(refresh_token, today_str)
        except Exception as e:
            logger.error(f"Error fetching calendar for morning briefing of {user.email}: {str(e)}")
            events = []

        # 2. Fetch CRM quota stats from Sheets
        goal_progress = "65%"  # Default fallback if sheet data is empty
        if user.spreadsheet_id:
            try:
                # Read sheets crm
                crm_data = GoogleSheetsService.read_crm_data(refresh_token, user.spreadsheet_id)
                # Check if there is a meta/precios sheet with metadata
                # For demo simplicity, look if we have some data or mock-calculate progress
                if crm_data.get("clientes"):
                    total_clients = len(crm_data["clientes"])
                    # Simple heuristic: calculate progress based on some row
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
            template_doc_id=user.template_doc_id
        )

        # Generate briefing text
        briefing_text, _ = agent.run([], prompt)

        # Get company credentials if the user belongs to one
        company_token = None
        phone_id = None
        if user.company_id:
            company = db.query(Company).filter(Company.id == user.company_id).first()
            if company:
                company_token = company.get_whatsapp_token()
                phone_id = company.whatsapp_phone_number_id

        # 4. Send briefing via WhatsApp (Using Template for Proactive Message)
        components = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": briefing_text}
                ]
            }
        ]
        
        # Replace 'morning_briefing' with the actual template name registered in Meta
        success = WhatsAppService.send_template_message(
            to_phone=user.phone_number, 
            template_name="morning_briefing",
            components=components,
            token=company_token,
            phone_id=phone_id
        )

        # Log agent response in DB
        db_log = ConversationLog(phone_number=user.phone_number, sender="agent", message=briefing_text)
        db.add(db_log)
        db.commit()

        results.append({
            "email": user.email,
            "success": success,
            "meetings_count": len(events)
        })

    return {"status": "completed", "processed_users": results}


@router.post("/evening-accountability")
def evening_accountability(db: Session = Depends(get_db)):
    """
    Triggered by Cloud Scheduler at 7:00 PM.
    Identifies meetings that took place today and initiates the Redis auditing state machine.
    """
    users = db.query(User).all()
    results = []

    for user in users:
        if not user.phone_number:
            continue

        logger.info(f"Running evening accountability check for {user.email}...")
        
        try:
            refresh_token = user.get_refresh_token()
        except Exception as e:
            logger.error(f"Failed to decrypt token for accountability check of {user.email}: {str(e)}")
            continue

        # Fetch today's agenda to find meetings
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            events = GoogleCalendarService.list_events(refresh_token, today_str)
        except Exception as e:
            logger.error(f"Error fetching calendar for accountability of {user.email}: {str(e)}")
            events = []

        if not events:
            # Salesperson had no meetings today, send a generic closing message
            msg = f"¡Hola {user.name}! He revisado tu agenda y no tenías reuniones hoy. ¡Espero que hayas tenido un día productivo!"
            WhatsAppService.send_text_message(user.phone_number, msg)
            results.append({"email": user.email, "status": "no_meetings_today"})
            continue

        # Get company credentials if the user belongs to one
        company_token = None
        phone_id = None
        if user.company_id:
            company = db.query(Company).filter(Company.id == user.company_id).first()
            if company:
                company_token = company.get_whatsapp_token()
                phone_id = company.whatsapp_phone_number_id

        # Find the most important or first meeting to audit
        audit_meeting = events[-1]
        meeting_id = audit_meeting["id"]
        meeting_summary = audit_meeting["summary"]
        
        # Save state machine in Redis
        # Expires in 4 hours
        redis_memory.set_state(
            phone_number=user.phone_number,
            state="AWAITING_MEETING_FEEDBACK",
            metadata={
                "meeting_id": meeting_id,
                "meeting_summary": meeting_summary
            },
            ttl=14400
        )

        # Construct interactive WhatsApp message with Yes/No buttons
        body_text = f"Hola {user.name}, ¿cómo te fue en tu junta de hoy: '{meeting_summary}'? ¿Se realizó la reunión?"
        
        # Proactive evening check using template
        components = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": body_text}
                ]
            }
        ]

        # Replace 'evening_accountability' with the actual template name registered in Meta
        success = WhatsAppService.send_template_message(
            to_phone=user.phone_number,
            template_name="evening_accountability",
            components=components,
            token=company_token,
            phone_id=phone_id
        )

        # Log agent message to DB
        db_log = ConversationLog(phone_number=user.phone_number, sender="agent", message=body_text)
        db.add(db_log)
        db.commit()

        results.append({
            "email": user.email,
            "status": "audit_initiated",
            "meeting_audited": meeting_summary,
            "success": success
        })

    return {"status": "completed", "processed_users": results}

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
        
    # Get company credentials if the user belongs to one
    company_token = None
    phone_id = None
    if user.company_id:
        company = db.query(Company).filter(Company.id == user.company_id).first()
        if company:
            company_token = company.get_whatsapp_token()
            phone_id = company.whatsapp_phone_number_id

    # For dynamic messages, we need a generic template or standard text if 24hr window is active
    # We will try sending a text message first (works if 24h window active), 
    # and if it fails or if we want to be safe, we could use a generic notification template.
    # For now, let's use send_text_message assuming the agent scheduled this within the 24h window,
    # or the user can configure a generic template.
    
    success = WhatsAppService.send_text_message(
        to_phone=user.phone_number,
        text=message,
        token=company_token,
        phone_id=phone_id
    )
    
    if success:
        db_log = ConversationLog(phone_number=user.phone_number, sender="agent", message=message)
        db.add(db_log)
        db.commit()
        return {"status": "ok"}
    else:
        return {"status": "failed", "message": "Failed to send WhatsApp message"}

@router.post("/daily_plan")
def daily_plan(db: Session = Depends(get_db)):
    """
    Triggered daily by Cloud Scheduler (e.g., 8:00 AM on workdays).
    Reads the seller's sales_goals and generates a daily plan via Gemini.
    """
    users = db.query(User).filter(User.sales_goals.isnot(None), User.sales_goals != "").all()
    results = []

    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    for user in users:
        if not user.phone_number:
            continue
            
        logger.info(f"Generating daily plan for user {user.email}...")
        
        try:
            refresh_token = user.get_refresh_token()
        except Exception as e:
            logger.error(f"Failed to decrypt token for daily plan of {user.email}: {str(e)}")
            continue

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
            template_doc_id=user.template_doc_id
        )

        plan_text, _ = agent.run([], prompt)

        company_token = None
        phone_id = None
        if user.company_id:
            company = db.query(Company).filter(Company.id == user.company_id).first()
            if company:
                company_token = company.get_whatsapp_token()
                phone_id = company.whatsapp_phone_number_id

        # We will use a generic template for proactive messaging
        # E.g., 'daily_plan_template' with 1 body text parameter
        components = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": plan_text[:1024]} # limit text size if needed by Meta
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
            db_log = ConversationLog(phone_number=user.phone_number, sender="agent", message=plan_text)
            db.add(db_log)
            db.commit()

        results.append({
            "email": user.email,
            "success": success
        })

    return {"status": "completed", "processed_users": results}
