import os
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from config.settings import settings

def get_user_credentials(refresh_token: str) -> Credentials:
    """
    Builds Google Credentials object from the user's refresh token and app credentials.
    Refreshes the credentials to ensure the access token is valid.
    """
    client_id = settings.GOOGLE_CLIENT_ID or os.getenv("GOOGLE_CLIENT_ID")
    client_secret = settings.GOOGLE_CLIENT_SECRET or os.getenv("GOOGLE_CLIENT_SECRET")
    
    if client_id: client_id = client_id.strip()
    if client_secret: client_secret = client_secret.strip()
    
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret
    )
    
    # Request a fresh access token
    creds.refresh(Request())
    return creds

def get_service_account_credentials():
    """
    Fallback for system-wide operations, loading from GOOGLE_APPLICATION_CREDENTIALS.
    """
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path and os.path.exists(cred_path):
        return service_account.Credentials.from_service_account_file(
            cred_path,
            scopes=[
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
        )
    return None
