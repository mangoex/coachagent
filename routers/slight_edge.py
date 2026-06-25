from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from datetime import datetime, date, timedelta
import json
import logging

from database.connection import get_db
from database.models import User, SlightEdgePlan, SlightEdgeLog
from config.settings import settings

try:
    import vertexai
    from vertexai.generative_models import GenerativeModel, Tool, Part, Content, FunctionDeclaration
    VERTEX_AVAILABLE = True
except ImportError:
    VERTEX_AVAILABLE = False

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/slight-edge", tags=["slight-edge"])

# Pydantic schemas
class ActivityConfigItem(BaseModel):
    activity: str
    points: int

class SlightEdgePlanCreateUpdate(BaseModel):
    monthly_income_goal: float
    ticket_average: float
    conversion_rate: float
    activities_config: List[ActivityConfigItem]
    funnel_metrics: Optional[Dict[str, Any]] = None
    daily_points_goal: int = 10

class SlightEdgeLogCreateUpdate(BaseModel):
    date_str: Optional[str] = None  # Formato YYYY-MM-DD
    completed_activities: Dict[str, int]

class CoachingChatRequest(BaseModel):
    message: str
    history: List[Dict[str, str]]


# Endpoints

@router.get("/plan/{user_id}")
def get_slight_edge_plan(user_id: int, db: Session = Depends(get_db)):
    """
    Obtiene el plan activo de La Ventaja para el usuario.
    """
    plan = db.query(SlightEdgePlan).filter(SlightEdgePlan.user_id == user_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="No se encontró un plan de La Ventaja para este usuario.")
    
    # Calculate actual stats for the current month (start of current month to today)
    today_dt = date.today()
    start_month = today_dt.replace(day=1)
    
    logs = db.query(SlightEdgeLog).filter(
        SlightEdgeLog.user_id == user_id,
        SlightEdgeLog.date >= start_month
    ).all()
    
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

    actual_calls = 0
    actual_meetings = 0
    actual_quotes = 0
    actual_sales = 0
    
    for log in logs:
        comp = log.completed_activities or {}
        for act_name, count in comp.items():
            cat = categorize_activity(act_name)
            val = max(0, count)
            if cat == "llamada":
                actual_calls += val
            elif cat == "cita":
                actual_meetings += val
            elif cat == "cotizacion":
                actual_quotes += val
            elif cat == "venta":
                actual_sales += val
                
    return {
        "id": plan.id,
        "user_id": plan.user_id,
        "monthly_income_goal": plan.monthly_income_goal,
        "ticket_average": plan.ticket_average,
        "conversion_rate": plan.conversion_rate,
        "funnel_metrics": plan.funnel_metrics,
        "actual_metrics": {
            "sales": actual_sales,
            "quotes": actual_quotes,
            "meetings": actual_meetings,
            "calls": actual_calls
        },
        "activities_config": plan.activities_config,
        "daily_points_goal": plan.daily_points_goal,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at
    }


@router.post("/plan/{user_id}")
def create_or_update_slight_edge_plan(user_id: int, payload: SlightEdgePlanCreateUpdate, db: Session = Depends(get_db)):
    """
    Crea o actualiza manualmente el plan de La Ventaja.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    plan = db.query(SlightEdgePlan).filter(SlightEdgePlan.user_id == user_id).first()
    if not plan:
        plan = SlightEdgePlan(user_id=user_id)
        db.add(plan)

    plan.monthly_income_goal = payload.monthly_income_goal
    plan.ticket_average = payload.ticket_average
    plan.conversion_rate = payload.conversion_rate
    plan.funnel_metrics = payload.funnel_metrics
    plan.activities_config = [item.dict() for item in payload.activities_config]
    plan.daily_points_goal = payload.daily_points_goal

    try:
        db.commit()
        db.refresh(plan)
        return {"status": "ok", "message": "Plan guardado exitosamente."}
    except Exception as e:
        db.rollback()
        logger.error(f"Error saving SlightEdgePlan: {e}")
        raise HTTPException(status_code=500, detail="Error al guardar el plan.")


@router.get("/log/{user_id}")
def get_slight_edge_logs(user_id: int, date_str: Optional[str] = None, db: Session = Depends(get_db)):
    """
    Obtiene los registros del usuario.
    Si se proporciona date_str, regresa solo el de ese día.
    De lo contrario, regresa el historial de los últimos 30 días para gráficas.
    """
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato de fecha inválido. Usar YYYY-MM-DD.")
        
        log = db.query(SlightEdgeLog).filter(
            SlightEdgeLog.user_id == user_id,
            SlightEdgeLog.date == target_date
        ).first()
        
        if not log:
            return {"date": date_str, "completed_activities": {}, "total_points": 0}
        
        return {
            "id": log.id,
            "user_id": log.user_id,
            "date": log.date.isoformat(),
            "completed_activities": log.completed_activities,
            "total_points": log.total_points
        }
    else:
        # Retornar historial de los últimos 30 días ordenado por fecha
        start_date = date.today() - timedelta(days=30)
        logs = db.query(SlightEdgeLog).filter(
            SlightEdgeLog.user_id == user_id,
            SlightEdgeLog.date >= start_date
        ).order_by(SlightEdgeLog.date.asc()).all()
        
        return [
            {
                "date": l.date.isoformat(),
                "completed_activities": l.completed_activities,
                "total_points": l.total_points
            } for l in logs
        ]


@router.post("/log/{user_id}")
def create_or_update_slight_edge_log(user_id: int, payload: SlightEdgeLogCreateUpdate, db: Session = Depends(get_db)):
    """
    Registra o actualiza el avance de actividades de un día y calcula el puntaje acumulado
    en base a las ponderaciones del plan activo.
    """
    plan = db.query(SlightEdgePlan).filter(SlightEdgePlan.user_id == user_id).first()
    if not plan:
        raise HTTPException(status_code=400, detail="Debes configurar tu plan de La Ventaja antes de registrar actividades.")

    # Resolver fecha
    if payload.date_str:
        try:
            target_date = datetime.strptime(payload.date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato de fecha inválido. Usar YYYY-MM-DD.")
    else:
        target_date = date.today()

    # Mapear pesos de actividades desde el plan
    weights = {item["activity"]: item["points"] for item in plan.activities_config}

    # Calcular puntos
    total_points = 0
    for act_name, count in payload.completed_activities.items():
        weight = weights.get(act_name, 0)
        total_points += max(0, count) * weight

    # Buscar o crear log
    log = db.query(SlightEdgeLog).filter(
        SlightEdgeLog.user_id == user_id,
        SlightEdgeLog.date == target_date
    ).first()

    if not log:
        log = SlightEdgeLog(
            user_id=user_id,
            date=target_date,
            completed_activities=payload.completed_activities,
            total_points=total_points
        )
        db.add(log)
    else:
        log.completed_activities = payload.completed_activities
        log.total_points = total_points

    try:
        db.commit()
        db.refresh(log)
        return {
            "status": "ok",
            "date": log.date.isoformat(),
            "completed_activities": log.completed_activities,
            "total_points": log.total_points
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Error saving SlightEdgeLog: {e}")
        raise HTTPException(status_code=500, detail="Error al guardar el registro diario.")


@router.post("/coaching-chat/{user_id}")
async def slight_edge_coaching_chat(user_id: int, payload: CoachingChatRequest, db: Session = Depends(get_db)):
    """
    Maneja el chat conversacional de coaching interactivo para estructurar el plan de La Ventaja.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    user_message = payload.message
    history = payload.history

    plan_saved = False

    # Declaración de la herramienta local para guardar el plan
    def save_slight_edge_plan(
        monthly_income_goal: float,
        ticket_average: float,
        conversion_rate: float,
        activities_config_json: str,
        funnel_metrics_json: str
    ) -> str:
        nonlocal plan_saved
        try:
            activities_config = json.loads(activities_config_json)
            funnel_metrics = json.loads(funnel_metrics_json)
            
            # Sanitizar activities_config
            formatted_config = []
            for item in activities_config:
                if isinstance(item, dict) and "activity" in item and "points" in item:
                    formatted_config.append({
                        "activity": str(item["activity"]),
                        "points": int(item["points"])
                    })
            
            plan = db.query(SlightEdgePlan).filter(SlightEdgePlan.user_id == user_id).first()
            if not plan:
                plan = SlightEdgePlan(user_id=user_id)
                db.add(plan)
            
            plan.monthly_income_goal = monthly_income_goal
            plan.ticket_average = ticket_average
            plan.conversion_rate = conversion_rate
            plan.activities_config = formatted_config
            plan.funnel_metrics = funnel_metrics
            plan.daily_points_goal = 10
            
            db.commit()
            plan_saved = True
            logger.info(f"SlightEdgePlan saved successfully for user {user_id}")
            return "¡Excelente! El plan de La Ligera Ventaja se ha guardado exitosamente en el sistema."
        except Exception as e:
            db.rollback()
            logger.error(f"Error in save_slight_edge_plan tool: {e}")
            return f"Error guardando el plan: {str(e)}"

    # Verificar disponibilidad de Vertex AI
    vertex_initialized = False
    if VERTEX_AVAILABLE:
        try:
            vertexai.init(project=settings.GCP_PROJECT_ID, location=settings.GCP_LOCATION)
            vertex_initialized = True
        except Exception as e:
            logger.warning(f"Could not initialize Vertex AI for coaching: {e}")

    if vertex_initialized:
        try:
            system_instruction = (
                "Eres un Coach de Ventas experto inspirado en las filosofías de 'La Ligera Ventaja' (The Slight Edge) de Jeff Olson y 'El Efecto Compuesto' (The Compound Effect) de Darren Hardy.\n"
                "Tu objetivo es guiar al vendedor en una sesión estructurada de coaching para definir sus objetivos y configurar un plan de actividades diarias medido por puntos.\n\n"
                "El principio fundamental es: pequeñas disciplinas diarias y consistentes, realizadas con buena actitud y confianza, se acumulan para generar un éxito masivo a largo plazo. Explícaselo al vendedor de forma motivadora.\n\n"
                "Sigue esta estructura rigurosa para la sesión:\n"
                "1. Da una cálida bienvenida enfocada en el Efecto Compuesto.\n"
                "2. Pregunta al vendedor su objetivo de ingresos mensuales deseado y el valor promedio de su ticket de venta (ticket promedio).\n"
                "3. Pregunta o estima sus indicadores clave de embudo (tasa de conversión de cotización a cierre, de cita a cotización, de llamada a cita, etc.). Si no los conoce, sugiérele valores estándar razonables (ej. 20% cotización-cierre, 50% cita-cotización, 10% llamada-cita).\n"
                "4. Realiza el cálculo matemático para obtener los objetivos diarios necesarios para alcanzar la meta (por ejemplo: si necesita 10 cierres al mes, requiere X cotizaciones, Y citas, Z llamadas).\n"
                "5. Diseña una propuesta de plan diario de actividades donde a cada una se le asigne un puntaje/peso por importancia (ej. Prospectar = 1pt, Llamar = 1pt, Citas = 2pt, Cotizar = 2pt, Seguimiento = 1pt, Cerrar = 3pt). El objetivo total diario sugerido debe ser de aproximadamente 10 puntos.\n"
                "6. Presenta este plan al usuario. Si el usuario está de acuerdo y aprueba el plan, DEBES llamar a la herramienta `save_slight_edge_plan` para guardar esta configuración en el sistema.\n\n"
                "Sé empático, altamente motivador, estructurado y enfocado en la consistencia diaria. Responde en español.\n"
            )

            # Preparar contenidos en formato Vertex
            contents = []
            for msg in history:
                role = "model" if msg.get("role") in ["agent", "model"] else "user"
                contents.append(Content(role=role, parts=[Part.from_text(msg.get("content"))]))

            contents.append(Content(role="user", parts=[Part.from_text(user_message)]))

            # Declaración de la herramienta
            save_plan_declaration = FunctionDeclaration(
                name="save_slight_edge_plan",
                description="Guarda el plan de actividades diarias y ponderaciones acordadas para el vendedor en la base de datos.",
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "monthly_income_goal": {"type": "NUMBER", "description": "Meta de ingresos mensuales del vendedor"},
                        "ticket_average": {"type": "NUMBER", "description": "Valor del ticket promedio de venta"},
                        "conversion_rate": {"type": "NUMBER", "description": "Tasa de conversión de cotización a cierre (ej: 0.20 para 20%)"},
                        "activities_config_json": {
                            "type": "STRING", 
                            "description": "Lista de actividades y sus puntos en formato JSON string, ejemplo: '[{\"activity\": \"Prospectar\", \"points\": 1}, {\"activity\": \"Hacer llamadas\", \"points\": 1}, {\"activity\": \"Generar citas\", \"points\": 2}, {\"activity\": \"Mandar cotizaciones\", \"points\": 2}, {\"activity\": \"Dar seguimiento\", \"points\": 1}, {\"activity\": \"Cerrar\", \"points\": 3}]'"
                        },
                        "funnel_metrics_json": {
                            "type": "STRING",
                            "description": "Métricas calculadas del embudo de ventas en formato JSON string, ejemplo: '{\"sales_needed\": 10, \"quotes_needed\": 50, \"meetings_needed\": 100, \"calls_needed\": 400}'"
                        }
                    },
                    "required": ["monthly_income_goal", "ticket_average", "conversion_rate", "activities_config_json", "funnel_metrics_json"]
                }
            )

            tools = Tool(function_declarations=[save_plan_declaration])
            model = GenerativeModel(
                model_name="gemini-2.5-pro",
                system_instruction=system_instruction,
                tools=[tools]
            )

            response = model.generate_content(contents)

            # Bucle de procesamiento de herramientas
            while response.candidates and response.candidates[0].function_calls:
                contents.append(response.candidates[0].content)
                tool_responses = []
                for call in response.candidates[0].function_calls:
                    name = call.name
                    args = dict(call.args)
                    logger.info(f"Coaching Agent tool call: {name} with args: {args}")
                    
                    if name == "save_slight_edge_plan":
                        res_val = save_slight_edge_plan(**args)
                    else:
                        res_val = "Herramienta no encontrada."
                        
                    tool_responses.append(
                        Part.from_function_response(
                            name=name,
                            response={"result": res_val}
                        )
                    )
                contents.append(Content(role="user", parts=tool_responses))
                response = model.generate_content(contents)

            final_reply = response.text or "Entendido."
            return {"reply": final_reply, "plan_saved": plan_saved}

        except Exception as ex:
            logger.exception("Error calling Vertex AI in coaching chat")
            # Fallback to mock

    # --- MOCK FALLBACK ---
    msg_lower = user_message.lower()
    full_chat = " ".join([m.get("content", "").lower() for m in history]) + " " + msg_lower

    if len(history) == 0:
        reply = (
            "¡Hola! Te doy la bienvenida a tu sesión de coaching de **La Ligera Ventaja**. "
            "Basándonos en la filosofía de Jeff Olson y Darren Hardy, sabemos que pequeñas disciplinas diarias y consistentes, "
            "hechas con buena actitud y confianza, se acumulan a lo largo del tiempo para generar resultados masivos.\n\n"
            "Para comenzar a diseñar tu plan personalizado, cuéntame: **¿Cuál es tu objetivo de ingresos mensuales deseado** "
            "y cuál es el **valor promedio (ticket promedio) de tu producto o servicio**?"
        )
    elif "ingresos" not in full_chat and "ticket" not in full_chat:
        reply = (
            "Para poder hacer tus cálculos de embudo de ventas, por favor indícame tu **meta de ingresos mensuales** "
            "y el **ticket promedio** de tus ventas."
        )
    elif "tasa" not in full_chat and "conversión" not in full_chat and "porcentaje" not in full_chat:
        reply = (
            "¡Excelente! Ahora hablemos de tus métricas de conversión. Si las conoces, indícame:\n"
            "- ¿Qué porcentaje de tus llamadas se convierten en citas?\n"
            "- ¿Qué porcentaje de tus citas se convierten en cotizaciones enviadas?\n"
            "- ¿Qué porcentaje de tus cotizaciones enviadas se cierran como ventas?\n\n"
            "*(Si no las conoces, no te preocupes, dime 'no sé' y utilizaremos las tasas de conversión promedio de tu industria)*."
        )
    elif "de acuerdo" not in msg_lower and "sí" not in msg_lower and "si" not in msg_lower and "aprobado" not in msg_lower and "ok" not in msg_lower:
        reply = (
            "¡Perfecto! Hemos realizado los cálculos de tu embudo. Para alcanzar tu meta propuesta, sugerimos el siguiente **Plan Diario de Actividades (Meta: 10 puntos diarios)**:\n\n"
            "📌 **Actividades propuestas y su peso**:\n"
            "- 🔎 **Prospectar**: 1 punto (Sugerido: 3 al día)\n"
            "- 📞 **Hacer llamadas**: 1 punto (Sugerido: 4 al día)\n"
            "- 📅 **Generar citas**: 2 puntos (Sugerido: 1 al día)\n"
            "- 📝 **Mandar cotizaciones**: 2 puntos (Sugerido: 1 al día)\n"
            "- 🔄 **Dar seguimiento**: 1 punto (Sugerido: 2 al día)\n"
            "- 🤝 **Cerrar**: 3 puntos\n"
            "- 💰 **Cobrar**: 2 puntos\n\n"
            "Si estás de acuerdo con esta propuesta de actividades y puntos para comenzar a acumular consistencia diaria, "
            "responde con **'Sí, estoy de acuerdo'** y guardaré tu plan en el sistema."
        )
    else:
        mock_config = [
            {"activity": "Prospectar", "points": 1},
            {"activity": "Hacer llamadas", "points": 1},
            {"activity": "Generar citas", "points": 2},
            {"activity": "Mandar cotizaciones", "points": 2},
            {"activity": "Dar seguimiento", "points": 1},
            {"activity": "Cerrar", "points": 3},
            {"activity": "Cobrar", "points": 2}
        ]
        mock_funnel = {
            "sales_needed": 10,
            "quotes_needed": 50,
            "meetings_needed": 100,
            "calls_needed": 400
        }
        
        save_slight_edge_plan(
            monthly_income_goal=10000.0,
            ticket_average=1000.0,
            conversion_rate=0.20,
            activities_config_json=json.dumps(mock_config),
            funnel_metrics_json=json.dumps(mock_funnel)
        )
        
        reply = (
            "¡Excelente elección! He guardado tu plan de **La Ligera Ventaja** en la base de datos de manera persistente.\n\n"
            "He configurado tu meta diaria en **10 puntos** con las actividades acordadas. "
            "El Efecto Compuesto ha comenzado. Ya puedes cerrar esta sesión y ver tu checklist diario en la pantalla."
        )

    return {"reply": reply, "plan_saved": plan_saved}
