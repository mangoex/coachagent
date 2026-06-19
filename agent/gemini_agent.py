import json
import logging
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime
import pytz
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

# Attempt to import vertexai; provide a fallback if it fails or is not authenticated
try:
    import vertexai
    from vertexai.generative_models import GenerativeModel, Tool, Part, Content, FunctionDeclaration
    VERTEX_AVAILABLE = True
except ImportError:
    VERTEX_AVAILABLE = False

from config.settings import settings
from services.calendar_service import GoogleCalendarService
from services.sheets_service import GoogleSheetsService
from services.docs_service import GoogleDocsService

logger = logging.getLogger(__name__)

class GeminiAgent:
    """
    Orchestrates Gemini tool calling and reasoning with Vertex AI.
    If Vertex AI is not initialized or fails, falls back to a simulated mock agent.
    """
    def __init__(self, user_refresh_token: str, spreadsheet_id: Optional[str] = None, template_doc_id: Optional[str] = None, sales_goals: Optional[str] = None, objectives: Optional[str] = None, calendar_id: str = "primary", phone_number: Optional[str] = None):
        self.refresh_token = user_refresh_token
        self.spreadsheet_id = spreadsheet_id
        self.template_doc_id = template_doc_id
        self.sales_goals = sales_goals
        self.objectives = objectives
        self.calendar_id = calendar_id
        self.phone_number = phone_number
        self.vertex_initialized = False

        if VERTEX_AVAILABLE:
            try:
                # Initialize Vertex AI SDK
                vertexai.init(project=settings.GCP_PROJECT_ID, location=settings.GCP_LOCATION)
                self._setup_tools()
                
                self.system_instruction = (
                    "Eres el 'Google AI Sales Coach Agent', un asistente de ventas proactivo y altamente capacitado.\n"
                    "Tu objetivo es ayudar a los vendedores a gestionar su agenda, dar seguimiento a clientes y automatizar cotizaciones.\n"
                    "Tienes acceso a herramientas de Google Workspace (Calendar, Sheets, Docs).\n"
                    "Cuando se te solicite leer el CRM o ver los clientes/productos/precios, lee el CRM en Google Sheets.\n"
                    "Cuando se apruebe una cotización, usa la herramienta generate_quotation para generar la propuesta y devuélvele al vendedor el enlace firmado del PDF resultante.\n"
                    "Tienes acceso a herramientas de accountability: si el vendedor te dice que hoy realizó llamadas, citas o propuestas, puedes usar la herramienta log_user_activities para registrarlas. También puedes usar get_user_accountability_progress para verificar cómo va el vendedor con respecto a sus metas diarias, semanales y mensuales y decírselo de forma motivadora y concisa.\n"
                    "Mantén un tono profesional, motivador, conciso y enfocado a objetivos comerciales. Responde en español.\n"
                )
                
                # Inyectar fecha y hora actual para evitar alucinaciones en base a la zona horaria del calendario del usuario
                try:
                    cal_tz = "America/Mexico_City"
                    if self.refresh_token:
                        metadata = GoogleCalendarService.get_calendar_metadata(self.refresh_token, self.calendar_id)
                        cal_tz = metadata.get("timeZone", "America/Mexico_City")
                    tz = pytz.timezone(cal_tz)
                except Exception as e:
                    logger.warning(f"Could not resolve dynamic timezone, falling back to America/Mexico_City: {e}")
                    tz = pytz.timezone('America/Mexico_City')

                now = datetime.now(tz)
                self.system_instruction += f"\n[Contexto del Sistema]\nHoy es: {now.strftime('%A, %d de %B de %Y')}.\nLa hora actual es: {now.strftime('%H:%M %Z')}.\n"
                self.system_instruction += "Toma en cuenta esta fecha y hora para todas tus acciones (por ejemplo, 'mañana a las 7am' o 'hoy a las 6pm'). Las fechas en las herramientas deben ir en formato ISO 8601.\n"
                
                if self.sales_goals:
                    self.system_instruction += f"\nMeta de Ventas del vendedor: {self.sales_goals}"
                if self.objectives:
                    self.system_instruction += f"\nObjetivos específicos: {self.objectives}"
                
                self.model = GenerativeModel(
                    model_name="gemini-2.5-pro",
                    system_instruction=self.system_instruction,
                    tools=[self.tools_list]
                )
                self.vertex_initialized = True
                logger.info("Vertex AI Gemini Agent initialized successfully.")
            except Exception as e:
                logger.warning(f"Could not initialize Vertex AI (using mock fallback): {str(e)}")
        else:
            logger.warning("Vertex AI library not installed or import failed. Running in MOCK mode.")

    def _setup_tools(self):
        """
        Defines the local python functions as Vertex AI tools.
        """
        def _check_auth() -> Optional[str]:
            if not self.refresh_token:
                return "ERROR CRÍTICO: El usuario no ha vinculado su cuenta de Google Workspace. DEBES responder diciéndole al usuario que vaya al panel web (la aplicación) y conecte su cuenta de Google Workspace desde la sección de 'Configuración' antes de poder usar el calendario o Google Docs."
            return None

        def list_calendar_events(date_str: str = "") -> str:
            """
            List calendar events for a specific day.
            
            Args:
                date_str: Date in 'YYYY-MM-DD' format. If empty, defaults to today.
            """
            auth_err = _check_auth()
            if auth_err: return auth_err
            try:
                events = GoogleCalendarService.list_events(self.refresh_token, date_str, self.calendar_id)
                return json.dumps(events, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Failed to list events: {str(e)}")
                return f"Error listing events: {str(e)}"

        def create_calendar_event(
            summary: str, 
            start_time_iso: str, 
            end_time_iso: str, 
            attendees_csv: str = "", 
            description: str = "",
            reminders_json: str = ""
        ) -> str:
            """
            Create a new event in Google Calendar.
            
            Args:
                summary: Title of the meeting.
                start_time_iso: Start date-time in ISO 8601 format (e.g. '2026-06-16T10:00:00Z').
                end_time_iso: End date-time in ISO 8601 format (e.g. '2026-06-16T11:00:00Z').
                attendees_csv: Comma-separated email addresses of attendees.
                description: Description of the meeting.
                reminders_json: Optional JSON string to customize notifications/reminders. If empty, the user's default calendar settings are used. Example override structure: '{"useDefault": false, "overrides": [{"method": "popup", "minutes": 15}]}'
            """
            auth_err = _check_auth()
            if auth_err: return auth_err
            try:
                attendees_list = [a.strip() for a in attendees_csv.split(",")] if attendees_csv else None
                reminders_dict = None
                if reminders_json:
                    try:
                        reminders_dict = json.loads(reminders_json)
                    except Exception as je:
                        logger.warning(f"Failed to parse reminders_json: {str(je)}")
                
                event = GoogleCalendarService.create_event(
                    refresh_token=self.refresh_token,
                    summary=summary,
                    start_time_iso=start_time_iso,
                    end_time_iso=end_time_iso,
                    attendees=attendees_list,
                    description=description,
                    calendar_id=self.calendar_id,
                    reminders=reminders_dict
                )
                return f"Event created: {event.get('htmlLink')}"
            except Exception as e:
                logger.error(f"Failed to create event: {str(e)}")
                return f"Error creating event: {str(e)}"

        def update_calendar_event(
            event_id: str, 
            summary: str = "", 
            start_time_iso: str = "", 
            end_time_iso: str = "", 
            attendees_csv: str = "", 
            description: str = "",
            reminders_json: str = ""
        ) -> str:
            """
            Update an existing calendar event.
            
            Args:
                event_id: The ID of the event to update.
                summary: New title of the meeting.
                start_time_iso: New start date-time in ISO 8601 format.
                end_time_iso: New end date-time in ISO 8601 format.
                attendees_csv: Comma-separated list of attendee emails.
                description: New description.
                reminders_json: Optional JSON string to customize notifications/reminders. If empty, reminders are not modified. Example structure: '{"useDefault": false, "overrides": [{"method": "popup", "minutes": 15}]}'
            """
            auth_err = _check_auth()
            if auth_err: return auth_err
            try:
                attendees_list = [a.strip() for a in attendees_csv.split(",")] if attendees_csv else None
                reminders_dict = None
                if reminders_json:
                    try:
                        reminders_dict = json.loads(reminders_json)
                    except Exception as je:
                        logger.warning(f"Failed to parse reminders_json: {str(je)}")

                event = GoogleCalendarService.update_event(
                    refresh_token=self.refresh_token,
                    event_id=event_id,
                    summary=summary or None,
                    start_time_iso=start_time_iso or None,
                    end_time_iso=end_time_iso or None,
                    attendees=attendees_list,
                    description=description or None,
                    calendar_id=self.calendar_id,
                    reminders=reminders_dict
                )
                return json.dumps(event, ensure_ascii=False)
            except Exception as e:
                return f"Error updating event: {str(e)}"

        def delete_calendar_event(event_id: str) -> str:
            """
            Delete an event from Google Calendar.
            
            Args:
                event_id: The ID of the event to delete.
            """
            auth_err = _check_auth()
            if auth_err: return auth_err
            try:
                success = GoogleCalendarService.delete_event(self.refresh_token, event_id, self.calendar_id)
                return "Event deleted successfully." if success else "Failed to delete event."
            except Exception as e:
                return f"Error deleting event: {str(e)}"

        def read_crm_data() -> str:
            """
            Read client list, products, and prices from the spreadsheet CRM.
            """
            auth_err = _check_auth()
            if auth_err: return auth_err
            if not self.spreadsheet_id:
                return "Error: No CRM spreadsheet ID configured for this user. Pídele al usuario que agregue el ID del documento en la configuración de la app."
            try:
                data = GoogleSheetsService.read_crm_data(self.refresh_token, self.spreadsheet_id)
                return json.dumps(data, ensure_ascii=False)
            except Exception as e:
                return f"Error reading CRM: {str(e)}"

        def generate_quotation(client_name: str, product_name: str, quantity: int, price: float, discount: float = 0.0) -> str:
            """
            Create a professional quotation document, export it to PDF, upload it to GCP storage, and return the signed URL.
            
            Args:
                client_name: Name of the customer.
                product_name: Name of the product.
                quantity: Quantity ordered.
                price: Unit price.
                discount: Discount percentage (e.g. 10.0 for 10% discount).
            """
            auth_err = _check_auth()
            if auth_err: return auth_err
            if not self.template_doc_id:
                return "Error: No template document ID configured for this user. Pídele al usuario que agregue el ID de la plantilla en la configuración."
            try:
                total = (price * quantity) * (1 - (discount / 100))
                replacements = {
                    "nombre_cliente": client_name,
                    "producto": product_name,
                    "cantidad": quantity,
                    "precio": price,
                    "descuento": f"{discount}%",
                    "total_cotizacion": f"${total:,.2f}"
                }
                url = GoogleDocsService.create_quote_from_template(
                    self.refresh_token, self.template_doc_id, replacements
                )
                return f"Quotation generated successfully. Signed PDF link: {url}"
            except Exception as e:
                return f"Error generating quotation: {str(e)}"

        def schedule_followup(message: str, scheduled_time_iso: str) -> str:
            """
            Schedule a proactive message to be sent to the user via WhatsApp at a specific future time.
            
            Args:
                message: The exact text message you want to send to the user.
                scheduled_time_iso: The precise date and time to send the message, in ISO 8601 format (e.g. 2023-12-01T15:30:00-06:00).
            """
            if not self.phone_number:
                return "Error: No phone number available to schedule the message."
            try:
                client = tasks_v2.CloudTasksClient()
                parent = client.queue_path(settings.GCP_PROJECT_ID, settings.GCP_LOCATION, settings.CLOUD_TASKS_QUEUE)
                
                url = f"{settings.BASE_URL.rstrip('/')}/cron/cloud-task-callback"
                payload = {"phone_number": self.phone_number, "message": message}
                
                task = {
                    "http_request": {
                        "http_method": tasks_v2.HttpMethod.POST,
                        "url": url,
                        "headers": {"Content-type": "application/json"},
                        "body": json.dumps(payload).encode()
                    }
                }
                
                # Set schedule time
                d = datetime.fromisoformat(scheduled_time_iso)
                timestamp = timestamp_pb2.Timestamp()
                timestamp.FromDatetime(d)
                task["schedule_time"] = timestamp
                
                response = client.create_task(request={"parent": parent, "task": task})
                return f"Follow-up message scheduled successfully. Task name: {response.name}"
            except Exception as e:
                logger.error(f"Error scheduling task: {str(e)}")
                return f"Error scheduling task: {str(e)}"

        def log_user_activities(citas: int = 0, llamadas: int = 0, propuestas: int = 0, date_str: str = "") -> str:
            """
            Registra o incrementa actividades de venta (citas, llamadas, propuestas) realizadas por el vendedor.
            Usa esta herramienta cuando el usuario te mencione que realizó llamadas, citas o propuestas.
            
            Args:
                citas: Número de citas a sumar (positivo) o establecer.
                llamadas: Número de llamadas a sumar (positivo) o establecer.
                propuestas: Número de propuestas a sumar (positivo) o establecer.
                date_str: Fecha opcional en formato 'YYYY-MM-DD'. Si está vacía, se asume hoy.
            """
            if not self.phone_number:
                return "Error: No se dispone del número telefónico para registrar la actividad."
            
            from database.connection import SessionLocal
            from database.models import User, DailyActivityLog
            from datetime import datetime
            import pytz

            db = SessionLocal()
            try:
                user = db.query(User).filter(User.phone_number == self.phone_number).first()
                if not user:
                    return f"Error: No se encontró vendedor registrado con el teléfono {self.phone_number}."

                # Determinar zona horaria
                cal_tz = "America/Mexico_City"
                if self.refresh_token:
                    try:
                        metadata = GoogleCalendarService.get_calendar_metadata(self.refresh_token, self.calendar_id)
                        cal_tz = metadata.get("timeZone", "America/Mexico_City")
                    except Exception:
                        pass
                tz = pytz.timezone(cal_tz)
                now_local = datetime.now(tz)

                if date_str:
                    try:
                        log_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    except ValueError:
                        return "Error: Formato de fecha inválido. Usar YYYY-MM-DD."
                else:
                    log_date = now_local.date()

                # Buscar o crear log para esa fecha
                log = db.query(DailyActivityLog).filter(
                    DailyActivityLog.user_id == user.id,
                    DailyActivityLog.date == log_date
                ).first()

                if not log:
                    log = DailyActivityLog(user_id=user.id, date=log_date)
                    db.add(log)
                    db.flush()

                # Incrementamos las actividades
                log.citas_completadas += citas
                log.llamadas_completadas += llamadas
                log.propuestas_completadas += propuestas
                db.commit()

                return (
                    f"Actividades registradas exitosamente para el día {log_date.isoformat()}:\n"
                    f"- Citas hoy: {log.citas_completadas} (se agregaron +{citas})\n"
                    f"- Llamadas hoy: {log.llamadas_completadas} (se agregaron +{llamadas})\n"
                    f"- Propuestas hoy: {log.propuestas_completadas} (se agregaron +{propuestas})\n"
                )
            except Exception as e:
                db.rollback()
                logger.error(f"Error log_user_activities tool: {e}")
                return f"Error registrando actividades: {str(e)}"
            finally:
                db.close()

        def get_user_accountability_progress() -> str:
            """
            Consulta el progreso actual de las metas y avance de actividades diarias, semanales y mensuales del vendedor.
            Úsala para informarle al vendedor cuántas llamadas, citas o propuestas lleva y qué porcentaje de su meta ha alcanzado.
            """
            if not self.phone_number:
                return "Error: No se dispone del número telefónico para consultar el progreso."

            from database.connection import SessionLocal
            from database.models import User, AccountabilityPlan, DailyActivityLog
            from datetime import datetime, timedelta
            import pytz

            db = SessionLocal()
            try:
                user = db.query(User).filter(User.phone_number == self.phone_number).first()
                if not user:
                    return f"Error: No se encontró vendedor registrado con el teléfono {self.phone_number}."

                cal_tz = "America/Mexico_City"
                if self.refresh_token:
                    try:
                        metadata = GoogleCalendarService.get_calendar_metadata(self.refresh_token, self.calendar_id)
                        cal_tz = metadata.get("timeZone", "America/Mexico_City")
                    except Exception:
                        pass
                tz = pytz.timezone(cal_tz)
                now_local = datetime.now(tz)
                today = now_local.date()

                start_month = today.replace(day=1)
                start_week = today - timedelta(days=today.weekday())

                # Consultar plan de metas
                plan = db.query(AccountabilityPlan).filter(AccountabilityPlan.user_id == user.id).first()
                if not plan:
                    return "Aún no has definido tus metas de actividades. Te sugiero crearlas en tu panel de control web."

                # Consultar logs de actividades
                logs = db.query(DailyActivityLog).filter(
                    DailyActivityLog.user_id == user.id,
                    DailyActivityLog.date >= start_month
                ).all()

                # Sumar acumulados
                today_acts = {"citas": 0, "llamadas": 0, "propuestas": 0}
                weekly_acts = {"citas": 0, "llamadas": 0, "propuestas": 0}
                monthly_acts = {"citas": 0, "llamadas": 0, "propuestas": 0}

                for log in logs:
                    if log.date == today:
                        today_acts["citas"] = log.citas_completadas
                        today_acts["llamadas"] = log.llamadas_completadas
                        today_acts["propuestas"] = log.propuestas_completadas
                    if log.date >= start_week:
                        weekly_acts["citas"] += log.citas_completadas
                        weekly_acts["llamadas"] += log.llamadas_completadas
                        weekly_acts["propuestas"] += log.propuestas_completadas
                    monthly_acts["citas"] += log.citas_completadas
                    monthly_acts["llamadas"] += log.llamadas_completadas
                    monthly_acts["propuestas"] += log.propuestas_completadas

                def report_section(name, actual, target):
                    if target <= 0:
                        return f"  - {name}: {actual} (meta no definida)"
                    pct = (actual / target) * 100
                    return f"  - {name}: {actual} de {target} ({pct:.1f}%)"

                res = (
                    f"Progreso de Accountability para {user.name} (Local: {today.isoformat()}):\n\n"
                    f"📅 METAS DIARIAS:\n"
                    f"{report_section('Citas', today_acts['citas'], plan.citas_meta_diaria)}\n"
                    f"{report_section('Llamadas', today_acts['llamadas'], plan.llamadas_meta_diaria)}\n"
                    f"{report_section('Propuestas', today_acts['propuestas'], plan.propuestas_meta_diaria)}\n\n"
                    f"📆 METAS SEMANALES:\n"
                    f"{report_section('Citas', weekly_acts['citas'], plan.citas_meta_semanal)}\n"
                    f"{report_section('Llamadas', weekly_acts['llamadas'], plan.llamadas_meta_semanal)}\n"
                    f"{report_section('Propuestas', weekly_acts['propuestas'], plan.propuestas_meta_semanal)}\n\n"
                    f"🗂 METAS MENSUALES:\n"
                    f"{report_section('Citas', monthly_acts['citas'], plan.citas_meta_mensual)}\n"
                    f"{report_section('Llamadas', monthly_acts['llamadas'], plan.llamadas_meta_mensual)}\n"
                    f"{report_section('Propuestas', monthly_acts['propuestas'], plan.propuestas_meta_mensual)}\n"
                )
                return res
            except Exception as e:
                logger.error(f"Error get_user_accountability_progress tool: {e}")
                return f"Error consultando progreso: {str(e)}"
            finally:
                db.close()

        self.tools_map = {
            "list_calendar_events": list_calendar_events,
            "create_calendar_event": create_calendar_event,
            "update_calendar_event": update_calendar_event,
            "delete_calendar_event": delete_calendar_event,
            "read_crm_data": read_crm_data,
            "generate_quotation": generate_quotation,
            "schedule_followup": schedule_followup,
            "log_user_activities": log_user_activities,
            "get_user_accountability_progress": get_user_accountability_progress
        }
        declarations = [FunctionDeclaration.from_func(func) for func in self.tools_map.values()]
        self.tools_list = Tool(function_declarations=declarations)

    def run(self, history: List[Dict[str, str]], user_message: str) -> Tuple[str, List[Dict[str, str]]]:
        """
        Runs the conversational loop with the user's message.
        Appends new messages to history and returns (final_reply_text, updated_history).
        """
        if not self.vertex_initialized:
            return self._run_mock(history, user_message)

        # Build Vertex AI content list from history
        contents = []
        for msg in history:
            role = msg.get("role")
            content = msg.get("content")
            # Gemini roles are strictly 'user' or 'model'
            gemini_role = "model" if role in ["agent", "model"] else "user"
            contents.append(Content(role=gemini_role, parts=[Part.from_text(content)]))

        # Add new user message
        contents.append(Content(role="user", parts=[Part.from_text(user_message)]))
        
        try:
            # Generate content
            response = self.model.generate_content(contents)
            
            # Keep executing tool calls returned by Gemini until it settles on a text response
            while response.candidates and response.candidates[0].function_calls:
                # Add model's request to contents
                contents.append(response.candidates[0].content)
                
                tool_responses = []
                for call in response.candidates[0].function_calls:
                    name = call.name
                    args = dict(call.args)
                    logger.info(f"Gemini requested tool call: {name} with args: {args}")
                    
                    if name in self.tools_map:
                        tool_result = self.tools_map[name](**args)
                    else:
                        tool_result = f"Tool '{name}' not found."
                        
                    tool_responses.append(
                        Part.from_function_response(
                            name=name,
                            response={"result": tool_result}
                        )
                    )
                
                # Send tool response back to the model
                contents.append(Content(role="user", parts=tool_responses))
                response = self.model.generate_content(contents)

            try:
                if response.candidates and response.candidates[0].function_calls:
                    final_reply = "¡Listo! He procesado tu solicitud."
                else:
                    final_reply = response.text
                    if not final_reply:
                        final_reply = "Entendido."
            except Exception:
                final_reply = "¡Listo! He procesado tu solicitud."
            
            # Map contents back to basic history format
            updated_history = []
            for item in contents:
                if not item.parts:
                    continue
                # Safely extract text, ignoring parts that are function calls/responses
                texts = []
                for part in item.parts:
                    try:
                        if getattr(part, 'function_call', None) or getattr(part, 'function_response', None):
                            continue
                        text_val = getattr(part, 'text', None)
                        if text_val:
                            texts.append(text_val)
                    except Exception:
                        pass
                
                if texts:
                    role = "user" if item.role == "user" else "agent"
                    updated_history.append({"role": role, "content": "\n".join(texts)})
            
            updated_history.append({"role": "agent", "content": final_reply})
            return final_reply, updated_history

        except Exception as e:
            logger.exception("Error in GeminiAgent execution")
            # Fallback to mock logic if live Vertex call fails
            return self._run_mock(history, user_message)

    def _run_mock(self, history: List[Dict[str, str]], user_message: str) -> Tuple[str, List[Dict[str, str]]]:
        """
        Simulates the agent's behavior for testing when Vertex AI credentials are not configured.
        """
        logger.info(f"[MOCK AGENT] Processing user message: '{user_message}'")
        msg_lower = user_message.lower()

        if "agenda" in msg_lower or "reunion" in msg_lower or "junta" in msg_lower:
            reply = (
                "Aquí tienes los eventos de hoy:\n"
                "- 10:00 AM: Reunión de cierre con Cliente X\n"
                "- 02:00 PM: Demo de producto con Cliente Y\n"
                "- 05:00 PM: Seguimiento semanal interno"
            )
        elif "crm" in msg_lower or "cliente" in msg_lower:
            reply = (
                "He revisado el CRM en Google Sheets:\n"
                "- Tienes 3 clientes activos (Juan Pérez, María Gómez, Carlos Ruiz)\n"
                "- Productos disponibles: Licencia SaaS ($100/mes), Soporte Premium ($500/mes)."
            )
        elif "cotiza" in msg_lower or "propuesta" in msg_lower:
            reply = (
                "He generado la cotización para Juan Pérez para el producto 'Licencia SaaS'.\n"
                "Puedes descargar el PDF aquí: https://storage.googleapis.com/mock-bucket/quotes/quote_mock123.pdf"
            )
        else:
            reply = (
                "Hola. Soy tu Sales Coach. Puedo ayudarte a consultar tu agenda, ver clientes del CRM "
                "o armar cotizaciones rápidamente. ¿Qué te gustaría hacer hoy?"
            )

        updated_history = list(history)
        updated_history.append({"role": "user", "content": user_message})
        updated_history.append({"role": "agent", "content": reply})
        return reply, updated_history
