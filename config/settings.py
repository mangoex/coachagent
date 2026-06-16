import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    # Base Configuration
    ENVIRONMENT: str = "development"
    GCP_PROJECT_ID: str = "my-gcp-project-id"
    GCP_LOCATION: str = "us-central1"
    
    # Encryption Key (Must be 32 URL-safe base64-encoded bytes for Fernet)
    ENCRYPTION_KEY: str = "zR_X_L8h_R64bJvFpQ3DfZ-YkC84e2c1eQ-F89x1a2A="

    # Database Settings
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/coachagent"

    # Redis Settings
    REDIS_URL: str = "redis://localhost:6379/0"

    # WhatsApp API Settings
    WHATSAPP_TOKEN: str = "EAAXxXX..."
    WHATSAPP_PHONE_NUMBER_ID: str = "1234567890"
    WHATSAPP_VERIFY_TOKEN: str = "my_secure_verify_token"

    # GCS Settings
    GCS_BUCKET_NAME: str = "coachagent-quotations-bucket"

    # Google OAuth client settings (For refresh token flow if credentials file is not present)
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
