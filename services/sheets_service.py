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
