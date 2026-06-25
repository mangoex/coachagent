from googleapiclient.discovery import build
from services.google_auth import get_user_credentials
from typing import List, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

class GoogleSheetsService:
    @staticmethod
    def _get_sheets_client(refresh_token: str):
        creds = get_user_credentials(refresh_token)
        return build("sheets", "v4", credentials=creds)

    @classmethod
    def get_sheet_values(cls, refresh_token: str, spreadsheet_id: str, range_name: str) -> List[List[Any]]:
        """
        Retrieves raw values from a specified Google Sheet range.
        """
        service = cls._get_sheets_client(refresh_token)
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=range_name
            ).execute()
            return result.get("values", [])
        except Exception as e:
            logger.error(f"Error fetching range {range_name} from sheet {spreadsheet_id}: {str(e)}")
            return []

    @classmethod
    def _parse_rows_to_dicts(cls, rows: List[List[Any]]) -> List[Dict[str, Any]]:
        """
        Helper to convert a grid of values (first row = headers) into a list of dicts.
        """
        if not rows:
            return []
        headers = [str(header).strip().lower().replace(" ", "_") for header in rows[0]]
        dicts = []
        for row in rows[1:]:
            # Pad row values if it has fewer elements than headers
            padded_row = row + [""] * (len(headers) - len(row))
            dicts.append(dict(zip(headers, padded_row[:len(headers)])))
        return dicts

    @classmethod
    def read_crm_data(cls, refresh_token: str, spreadsheet_id: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Reads Clientes, Productos, and Precios tabs from the sheet and returns them as parsed dicts.
        """
        crm_data = {
            "clientes": [],
            "productos": [],
            "precios": []
        }

        # Fetch Clientes
        clientes_raw = cls.get_sheet_values(refresh_token, spreadsheet_id, "Clientes!A1:Z1000")
        crm_data["clientes"] = cls._parse_rows_to_dicts(clientes_raw)

        # Fetch Productos
        productos_raw = cls.get_sheet_values(refresh_token, spreadsheet_id, "Productos!A1:Z1000")
        crm_data["productos"] = cls._parse_rows_to_dicts(productos_raw)

        # Fetch Precios (Optional/Merged if same tab, otherwise parsed separately)
        precios_raw = cls.get_sheet_values(refresh_token, spreadsheet_id, "Precios!A1:Z1000")
        crm_data["precios"] = cls._parse_rows_to_dicts(precios_raw)

        return crm_data

    @classmethod
    def append_crm_client(cls, refresh_token: str, spreadsheet_id: str, client_name: str, client_email: str, client_phone: str, notes: str = "", status: str = "Nuevo") -> str:
        """
        Appends a new client row to the 'Clientes' sheet.
        """
        service = cls._get_sheets_client(refresh_token)
        try:
            # We assume columns: Nombre, Email, Teléfono, Estado, Notas (or similar)
            values = [[client_name, client_email, client_phone, status, notes]]
            body = {
                "values": values
            }
            # Append to the bottom of Clientes tab
            result = service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range="Clientes!A2",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body=body
            ).execute()
            updated_range = result.get("updates", {}).get("updatedRange", "Clientes")
            logger.info(f"Appended client {client_name} to sheet {spreadsheet_id}, range: {updated_range}")
            return f"Cliente '{client_name}' agregado exitosamente al CRM de Google Sheets."
        except Exception as e:
            logger.error(f"Error appending client to sheet {spreadsheet_id}: {str(e)}")
            return f"Error al agregar cliente en Google Sheets: {str(e)}"

    @classmethod
    def update_crm_client_status(cls, refresh_token: str, spreadsheet_id: str, client_phone_or_email: str, new_status: str, notes: Optional[str] = None) -> str:
        """
        Searches for a client by phone number or email and updates their Status (column D) and optionally Notes (column E).
        Assumes columns: A=Nombre, B=Email, C=Teléfono, D=Estado, E=Notas
        """
        service = cls._get_sheets_client(refresh_token)
        try:
            # Fetch all existing clients (read up to row 1000)
            raw_values = cls.get_sheet_values(refresh_token, spreadsheet_id, "Clientes!A1:E1000")
            if not raw_values:
                return "No se encontraron datos en la hoja de Clientes."
            
            search_key = client_phone_or_email.lower().strip()
            
            # Find row index (1-indexed for Sheets API)
            row_idx = -1
            for idx, row in enumerate(raw_values):
                if idx == 0:  # Skip headers
                    continue
                # Row columns: 0=Nombre, 1=Email, 2=Teléfono
                email_val = row[1].lower().strip() if len(row) > 1 else ""
                phone_val = str(row[2]).strip() if len(row) > 2 else ""
                
                # Check match
                if search_key in email_val or search_key in phone_val or (phone_val and search_key.replace("+", "") in phone_val.replace("+", "")):
                    row_idx = idx + 1 # 1-based index
                    break
            
            if row_idx == -1:
                return f"No se encontró ningún cliente en el CRM con el correo o teléfono: {client_phone_or_email}"
            
            # Update status in column D (which is index 4, i.e. column 'D')
            body_status = {
                "values": [[new_status]]
            }
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"Clientes!D{row_idx}",
                valueInputOption="USER_ENTERED",
                body=body_status
            ).execute()
            
            # Optionally update notes in column E (index 5)
            if notes:
                body_notes = {
                    "values": [[notes]]
                }
                service.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range=f"Clientes!E{row_idx}",
                    valueInputOption="USER_ENTERED",
                    body=body_notes
                ).execute()
                
            return f"Estado de cliente '{client_phone_or_email}' actualizado a '{new_status}' en la fila {row_idx}."
        except Exception as e:
            logger.error(f"Error updating client status in sheet {spreadsheet_id}: {str(e)}")
            return f"Error al actualizar cliente en Google Sheets: {str(e)}"
