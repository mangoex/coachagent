from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database.connection import get_db
from database.models import User
from services.calendar_service import GoogleCalendarService
from services.sheets_service import GoogleSheetsService
from services.docs_service import GoogleDocsService

router = APIRouter(prefix="/api/audit", tags=["audit"])

@router.get("/{email}")
def perform_audit(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    try:
        refresh_token = user.get_refresh_token()
        if not refresh_token:
            raise ValueError("Token is empty")
    except Exception:
        return {
            "calendar": {"status": "error", "message": "No hay token válido"},
            "sheets": {"status": "error", "message": "No hay token válido"},
            "docs": {"status": "error", "message": "No hay token válido"}
        }

    results = {}

    # Check Calendar
    try:
        calendars = GoogleCalendarService.list_calendars(refresh_token)
        results["calendar"] = {"status": "ok", "message": f"Conectado. {len(calendars)} calendarios encontrados."}
    except Exception as e:
        results["calendar"] = {"status": "error", "message": str(e)}

    # Check Sheets
    if user.spreadsheet_id:
        try:
            GoogleSheetsService.read_crm_data(refresh_token, user.spreadsheet_id)
            results["sheets"] = {"status": "ok", "message": f"Conectado a CRM."}
        except Exception as e:
            results["sheets"] = {"status": "error", "message": str(e)}
    else:
        results["sheets"] = {"status": "warning", "message": "No se ha configurado un ID de Spreadsheet."}

    # Check Docs
    if user.template_doc_id:
        try:
            docs_client = GoogleDocsService._get_docs_client(refresh_token)
            doc = docs_client.documents().get(documentId=user.template_doc_id).execute()
            title = doc.get("title", "Desconocido")
            results["docs"] = {"status": "ok", "message": f"Conectado a plantilla: '{title}'"}
        except Exception as e:
            results["docs"] = {"status": "error", "message": str(e)}
    else:
        results["docs"] = {"status": "warning", "message": "No se ha configurado un ID de Plantilla."}

    return results
