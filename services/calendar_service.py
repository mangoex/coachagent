from datetime import datetime, time, timedelta
from googleapiclient.discovery import build
from services.google_auth import get_user_credentials
from typing import List, Optional, Dict, Any

class GoogleCalendarService:
    @staticmethod
    def _get_calendar_client(refresh_token: str):
        creds = get_user_credentials(refresh_token)
        return build("calendar", "v3", credentials=creds)

    @classmethod
    def list_events(cls, refresh_token: str, date_str: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List events for a specific day.
        :param date_str: String format 'YYYY-MM-DD'. If None, defaults to today.
        """
        service = cls._get_calendar_client(refresh_token)
        
        if date_str:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        else:
            target_date = datetime.utcnow().date()

        # Define bounds of the day in RFC3339 format
        time_min = datetime.combine(target_date, time.min).isoformat() + "Z"
        time_max = datetime.combine(target_date, time.max).isoformat() + "Z"

        events_result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        events = events_result.get("items", [])
        
        parsed_events = []
        for event in events:
            parsed_events.append({
                "id": event.get("id"),
                "summary": event.get("summary", "Sin Título"),
                "description": event.get("description", ""),
                "start": event.get("start", {}).get("dateTime") or event.get("start", {}).get("date"),
                "end": event.get("end", {}).get("dateTime") or event.get("end", {}).get("date"),
                "attendees": [a.get("email") for a in event.get("attendees", []) if a.get("email")],
                "status": event.get("status")
            })
        return parsed_events

    @classmethod
    def create_event(
        cls, 
        refresh_token: str, 
        summary: str, 
        start_time_iso: str, 
        end_time_iso: str, 
        attendees: Optional[List[str]] = None,
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a new event in Google Calendar.
        :param start_time_iso: ISO 8601 string, e.g., '2026-06-16T10:00:00-07:00'
        :param end_time_iso: ISO 8601 string, e.g., '2026-06-16T11:00:00-07:00'
        """
        service = cls._get_calendar_client(refresh_token)
        
        event_body = {
            "summary": summary,
            "description": description or "Creado por Google AI Sales Coach Agent",
            "start": {"dateTime": start_time_iso, "timeZone": "UTC"},
            "end": {"dateTime": end_time_iso, "timeZone": "UTC"},
        }
        
        if attendees:
            event_body["attendees"] = [{"email": email} for email in attendees]

        created_event = service.events().insert(
            calendarId="primary",
            body=event_body,
            sendUpdates="all"
        ).execute()

        return {
            "id": created_event.get("id"),
            "summary": created_event.get("summary"),
            "start": created_event.get("start", {}).get("dateTime"),
            "end": created_event.get("end", {}).get("dateTime"),
            "htmlLink": created_event.get("htmlLink")
        }

    @classmethod
    def update_event(
        cls, 
        refresh_token: str, 
        event_id: str, 
        summary: Optional[str] = None, 
        start_time_iso: Optional[str] = None, 
        end_time_iso: Optional[str] = None,
        attendees: Optional[List[str]] = None,
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Update an existing event.
        """
        service = cls._get_calendar_client(refresh_token)
        
        # Get existing event first to merge updates
        event = service.events().get(calendarId="primary", eventId=event_id).execute()

        if summary is not None:
            event["summary"] = summary
        if description is not None:
            event["description"] = description
        if start_time_iso is not None:
            event["start"] = {"dateTime": start_time_iso}
        if end_time_iso is not None:
            event["end"] = {"dateTime": end_time_iso}
        if attendees is not None:
            event["attendees"] = [{"email": email} for email in attendees]

        updated_event = service.events().update(
            calendarId="primary",
            eventId=event_id,
            body=event,
            sendUpdates="all"
        ).execute()

        return {
            "id": updated_event.get("id"),
            "summary": updated_event.get("summary"),
            "start": updated_event.get("start", {}).get("dateTime"),
            "end": updated_event.get("end", {}).get("dateTime")
        }

    @classmethod
    def delete_event(cls, refresh_token: str, event_id: str) -> bool:
        """
        Delete an event from primary calendar.
        """
        service = cls._get_calendar_client(refresh_token)
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return True
