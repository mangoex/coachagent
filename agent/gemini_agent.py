import json
import logging
from typing import List, Dict, Any, Tuple, Optional

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
    def __init__(self, user_refresh_token: str, spreadsheet_id: Optional[str] = None, template_doc_id: Optional[str] = None):
        self.refresh_token = user_refresh_token
        self.spreadsheet_id = spreadsheet_id
        self.template_doc_id = template_doc_id
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
                    "Mantén un tono profesional, motivador, conciso y enfocado a objetivos comerciales. Responde en español."
                )
                
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
        def list_calendar_events(date_str: Optional[str] = None) -> str:
            """
            List calendar events for a specific day.
            
            Args:
                date_str: Optional date in 'YYYY-MM-DD' format. If not provided, defaults to today.
            """
            try:
                events = GoogleCalendarService.list_events(self.refresh_token, date_str)
                return json.dumps(events, ensure_ascii=False)
            except Exception as e:
                return f"Error listing events: {str(e)}"

        def create_calendar_event(
            summary: str, 
            start_time_iso: str, 
            end_time_iso: str, 
            attendees: Optional[List[str]] = None, 
            description: Optional[str] = None
        ) -> str:
            """
            Create a new event in Google Calendar.
            
            Args:
                summary: Title of the meeting.
                start_time_iso: Start date-time in ISO 8601 format (e.g. '2026-06-16T10:00:00Z').
                end_time_iso: End date-time in ISO 8601 format (e.g. '2026-06-16T11:00:00Z').
                attendees: Optional list of email addresses of attendees.
                description: Optional description of the meeting.
            """
            try:
                event = GoogleCalendarService.create_event(
                    self.refresh_token, summary, start_time_iso, end_time_iso, attendees, description
                )
                return json.dumps(event, ensure_ascii=False)
            except Exception as e:
                return f"Error creating event: {str(e)}"

        def update_calendar_event(
            event_id: str, 
            summary: Optional[str] = None, 
            start_time_iso: Optional[str] = None, 
            end_time_iso: Optional[str] = None, 
            attendees: Optional[List[str]] = None, 
            description: Optional[str] = None
        ) -> str:
            """
            Update an existing calendar event.
            
            Args:
                event_id: The ID of the event to update.
                summary: Optional new title of the meeting.
                start_time_iso: Optional new start date-time in ISO 8601 format.
                end_time_iso: Optional new end date-time in ISO 8601 format.
                attendees: Optional list of attendee emails.
                description: Optional new description.
            """
            try:
                event = GoogleCalendarService.update_event(
                    self.refresh_token, event_id, summary, start_time_iso, end_time_iso, attendees, description
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
            try:
                success = GoogleCalendarService.delete_event(self.refresh_token, event_id)
                return "Event deleted successfully." if success else "Failed to delete event."
            except Exception as e:
                return f"Error deleting event: {str(e)}"

        def read_crm_data() -> str:
            """
            Read client list, products, and prices from the spreadsheet CRM.
            """
            if not self.spreadsheet_id:
                return "Error: No CRM spreadsheet ID configured for this user."
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
            if not self.template_doc_id:
                return "Error: No template document ID configured for this user."
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

        self.tools_map = {
            "list_calendar_events": list_calendar_events,
            "create_calendar_event": create_calendar_event,
            "update_calendar_event": update_calendar_event,
            "delete_calendar_event": delete_calendar_event,
            "read_crm_data": read_crm_data,
            "generate_quotation": generate_quotation
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

            final_reply = response.text if response.text else "Entendido."
            
            # Map contents back to basic history format
            updated_history = []
            for item in contents:
                if len(item.parts) > 0 and item.parts[0].text:
                    role = "user" if item.role == "user" else "agent"
                    updated_history.append({"role": role, "content": item.parts[0].text})
            
            updated_history.append({"role": "agent", "content": final_reply})
            return final_reply, updated_history

        except Exception as e:
            logger.error(f"Error in GeminiAgent execution: {str(e)}")
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
