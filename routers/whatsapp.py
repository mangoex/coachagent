from fastapi import APIRouter, Request, Response, Depends, status
from sqlalchemy.orm import Session
import logging
import json

from config.settings import settings
from database.connection import get_db
from database.models import User, ConversationLog, Company, CalendarEventAudit, DailyActivityLog
from services.whatsapp_service import WhatsAppService
from services.calendar_service import GoogleCalendarService
from agent.redis_memory import redis_memory
from agent.gemini_agent import GeminiAgent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["whatsapp"])

@router.get("/whatsapp")
def verify_webhook(request: Request):
    """
    Verification endpoint for Meta WhatsApp webhook.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == settings.WHATSAPP_VERIFY_TOKEN:
            logger.info("WhatsApp webhook verified successfully.")
            return Response(content=challenge, media_type="text/plain")
        else:
            logger.warning("WhatsApp verification failed. Token mismatch.")
            return Response(content="Forbidden", status_code=status.HTTP_403_FORBIDDEN)
    return Response(content="Bad Request", status_code=status.HTTP_400_BAD_REQUEST)

@router.post("/whatsapp")
async def receive_message(request: Request, db: Session = Depends(get_db)):
    """
    Receiver webhook for incoming WhatsApp messages and interactive button clicks.
    """
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON payload"}

    logger.info(f"Incoming WhatsApp webhook payload: {json.dumps(body)}")

    # Extract messages list from Meta payload format
    entry = body.get("entry", [])
    if not entry:
        return {"status": "ok"}
    
    changes = entry[0].get("changes", [])
    if not changes:
        return {"status": "ok"}
        
    value = changes[0].get("value", {})
    messages = value.get("messages", [])
    if not messages:
        # Might be a status update callback (sent, delivered, read), which we ignore for now
        return {"status": "ok"}

    msg_data = messages[0]
    from_phone = msg_data.get("from")  # Sender's WhatsApp number
    msg_id = msg_data.get("id")
    
    # Extract destination phone number ID from Meta payload
    metadata = value.get("metadata", {})
    dest_phone_id = metadata.get("phone_number_id")
    
    # Resolve company credentials if any
    company_token = None
    if dest_phone_id and dest_phone_id != settings.WHATSAPP_PHONE_NUMBER_ID:
        company = db.query(Company).filter(Company.whatsapp_phone_number_id == dest_phone_id).first()
        if company:
            company_token = company.get_whatsapp_token()

    # Find the salesperson/tenant associated with this phone number
    user = db.query(User).filter(User.phone_number == from_phone).first()
    if not user:
        logger.warning(f"Message received from unregistered phone number: {from_phone}")
        # Optionally send a default warning message
        WhatsAppService.send_text_message(
            to_phone=from_phone,
            text="Hola. Tu número de teléfono no está registrado en el sistema de Google AI Sales Coach Agent.",
            token=company_token,
            phone_id=dest_phone_id
        )
        return {"status": "ok"}

    # Decrypt their refresh token
    try:
        refresh_token = user.get_refresh_token()
    except Exception as e:
        logger.error(f"Failed to decrypt refresh token for user {user.email}: {str(e)}")
        WhatsAppService.send_text_message(
            to_phone=from_phone,
            text="Error de autenticación. Por favor, re-conecta tus credenciales de Google Workspace.",
            token=company_token,
            phone_id=dest_phone_id
        )
        return {"status": "ok"}

    # Log incoming message to DB
    incoming_text = ""
    is_interactive = False
    button_id = None
    
    if msg_data.get("type") == "text":
        incoming_text = msg_data.get("text", {}).get("body", "")
    elif msg_data.get("type") == "interactive":
        is_interactive = True
        interactive_data = msg_data.get("interactive", {})
        if interactive_data.get("type") == "button_reply":
            button_reply = interactive_data.get("button_reply", {})
            button_id = button_reply.get("id", "")
            incoming_text = button_reply.get("title", "")
            logger.info(f"Interactive button clicked: {button_id} ({incoming_text})")

    db_log_in = ConversationLog(phone_number=from_phone, sender="user", message=incoming_text)
    db.add(db_log_in)
    db.commit()

    # Check state machine in Redis
    state, state_meta = redis_memory.get_state(from_phone)

    # 1. Process accountability state machine responses
    if state == "AWAITING_MEETING_FEEDBACK" and is_interactive and button_id:
        meeting_id = state_meta.get("meeting_id")
        meeting_summary = state_meta.get("meeting_summary", "reunión")
        
        # Extract event_id from button_id if formatted as btn_yes_eventid / btn_no_eventid
        event_id = meeting_id
        if button_id.startswith("btn_yes_"):
            event_id = button_id[8:]
        elif button_id.startswith("btn_no_"):
            event_id = button_id[7:]
            
        from database.models import CalendarEventAudit, DailyActivityLog
        import pytz
        from datetime import datetime
        
        audit = db.query(CalendarEventAudit).filter(
            CalendarEventAudit.user_id == user.id,
            CalendarEventAudit.event_id == event_id
        ).first()
        
        was_completed = audit.is_completed if audit else False
        
        if button_id.startswith("btn_yes"):
            # Mark event completed in DB
            if not audit:
                audit = CalendarEventAudit(
                    user_id=user.id,
                    event_id=event_id,
                    event_summary=meeting_summary,
                    is_completed=True,
                    audit_status="confirmed"
                )
                db.add(audit)
            else:
                audit.is_completed = True
                audit.audit_status = "confirmed"
            db.commit()
            
            # Increment DailyActivityLog
            if not was_completed:
                cal_tz = "America/Mexico_City"
                try:
                    metadata = GoogleCalendarService.get_calendar_metadata(refresh_token, user.calendar_id)
                    cal_tz = metadata.get("timeZone", "America/Mexico_City")
                except Exception:
                    pass
                tz = pytz.timezone(cal_tz)
                if audit and audit.event_start:
                    dt_local = audit.event_start.astimezone(tz) if audit.event_start.tzinfo else tz.localize(audit.event_start)
                    log_date = dt_local.date()
                else:
                    log_date = datetime.now(tz).date()
                    
                log = db.query(DailyActivityLog).filter(
                    DailyActivityLog.user_id == user.id,
                    DailyActivityLog.date == log_date
                ).first()
                if not log:
                    log = DailyActivityLog(user_id=user.id, date=log_date, citas_completadas=0, llamadas_completadas=0, propuestas_completadas=0)
                    db.add(log)
                log.citas_completadas += 1
                db.commit()
                
            # Clear state
            redis_memory.clear_state(from_phone)
            reply_text = (
                f"¡Excelente noticia! He registrado tu cita '{meeting_summary}' como realizada y la sumé a tus metas de hoy.\n\n"
                "¿Necesitas generar una cotización para este cliente? Si es así, dime el nombre del cliente, "
                "producto, cantidad y precio base. ¡Yo me encargo del resto!"
            )
            WhatsAppService.send_text_message(from_phone, reply_text, token=company_token, phone_id=dest_phone_id)
            
            # Log reply to DB
            db_log_out = ConversationLog(phone_number=from_phone, sender="agent", message=reply_text)
            db.add(db_log_out)
            db.commit()
            return {"status": "ok"}
            
        elif button_id.startswith("btn_no"):
            # Mark event not completed in DB
            if not audit:
                audit = CalendarEventAudit(
                    user_id=user.id,
                    event_id=event_id,
                    event_summary=meeting_summary,
                    is_completed=False,
                    audit_status="no_show"
                )
                db.add(audit)
            else:
                audit.is_completed = False
                audit.audit_status = "no_show"
            db.commit()
            
            # Clear state
            redis_memory.clear_state(from_phone)
            
            # Propose rescheduling slot
            reply_text = (
                f"Entendido. He marcado la cita '{meeting_summary}' como no realizada en tu registro.\n\n"
                "Revisando tu agenda de Google Calendar, he encontrado estos espacios disponibles para mañana:\n"
                "- 10:00 AM - 11:00 AM\n"
                "- 03:00 PM - 04:00 PM\n\n"
                "Dime cuál prefieres para agendarlo de inmediato."
            )
            WhatsAppService.send_text_message(from_phone, reply_text, token=company_token, phone_id=dest_phone_id)
            
            # Log reply to DB
            db_log_out = ConversationLog(phone_number=from_phone, sender="agent", message=reply_text)
            db.add(db_log_out)
            db.commit()
            return {"status": "ok"}


    # 2. Default flow: Send message to Cognitive Gemini Agent
    chat_history = redis_memory.get_history(from_phone)
    
    agent = GeminiAgent(
        user_refresh_token=refresh_token,
        spreadsheet_id=user.spreadsheet_id,
        template_doc_id=user.template_doc_id,
        sales_goals=user.sales_goals,
        objectives=user.objectives,
        calendar_id=user.calendar_id,
        phone_number=from_phone
    )

    # Run agent cycle (includes tool executions)
    agent_reply, updated_history = agent.run(chat_history, incoming_text)

    # Save conversation state in Redis
    redis_memory.add_message(from_phone, "user", incoming_text)
    redis_memory.add_message(from_phone, "agent", agent_reply)

    # Send message to WhatsApp
    WhatsAppService.send_text_message(from_phone, agent_reply, token=company_token, phone_id=dest_phone_id)

    # Log agent response to DB
    db_log_out = ConversationLog(phone_number=from_phone, sender="agent", message=agent_reply)
    db.add(db_log_out)
    db.commit()

    return {"status": "ok"}
