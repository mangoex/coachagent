import sys
import logging
import traceback
from database.connection import SessionLocal
from database.models import User
from agent.gemini_agent import GeminiAgent
from services.calendar_service import GoogleCalendarService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

db = SessionLocal()
user = db.query(User).filter(User.email == 'mangoex@gmail.com').first()
if not user:
    print("User not found!")
    sys.exit(1)

# Map user to dict as expected by GeminiAgent
seller_data = {
    "email": user.email,
    "encrypted_refresh_token": user.encrypted_refresh_token
}

print(f"Token: {seller_data.get('encrypted_refresh_token')}")

agent = GeminiAgent(seller_data)

try:
    events = GoogleCalendarService.list_events(agent.refresh_token, "")
    print(f"Events: {events}")
except Exception as e:
    traceback.print_exc()
    print(f"Failed to list events: {e}")
