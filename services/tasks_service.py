from googleapiclient.discovery import build
from services.google_auth import get_user_credentials
from typing import List, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

class GoogleTasksService:
    @staticmethod
    def _get_tasks_client(refresh_token: str):
        creds = get_user_credentials(refresh_token)
        return build("tasks", "v1", credentials=creds)

    @classmethod
    def create_task(
        cls, 
        refresh_token: str, 
        title: str, 
        notes: Optional[str] = None, 
        due_date_iso: Optional[str] = None, 
        tasklist_id: str = "@default"
    ) -> Dict[str, Any]:
        """
        Create a new task in Google Tasks.
        :param due_date_iso: ISO 8601 string format. Needs to be RFC 3339 timestamp with mandatory time zone offset, e.g., 2011-04-05T08:00:00.000Z.
        """
        try:
            service = cls._get_tasks_client(refresh_token)
            task_body = {"title": title}
            
            if notes:
                task_body["notes"] = notes
                
            if due_date_iso:
                task_body["due"] = due_date_iso
                
            created_task = service.tasks().insert(tasklist=tasklist_id, body=task_body).execute()
            return created_task
        except Exception as e:
            logger.error(f"Error creating Google Task: {e}")
            raise

    @classmethod
    def list_tasks(cls, refresh_token: str, tasklist_id: str = "@default", show_completed: bool = False) -> List[Dict[str, Any]]:
        """
        List tasks from the specified task list.
        """
        try:
            service = cls._get_tasks_client(refresh_token)
            result = service.tasks().list(tasklist=tasklist_id, showCompleted=show_completed, showHidden=False).execute()
            return result.get("items", [])
        except Exception as e:
            logger.error(f"Error listing Google Tasks: {e}")
            raise

    @classmethod
    def complete_task(cls, refresh_token: str, task_id: str, tasklist_id: str = "@default") -> Dict[str, Any]:
        """
        Mark a Google Task as completed.
        """
        try:
            service = cls._get_tasks_client(refresh_token)
            task_body = {
                "status": "completed"
            }
            updated_task = service.tasks().patch(
                tasklist=tasklist_id,
                task=task_id,
                body=task_body
            ).execute()
            logger.info(f"Marked Google Task {task_id} as completed.")
            return updated_task
        except Exception as e:
            logger.error(f"Error completing Google Task {task_id}: {e}")
            raise
