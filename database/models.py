from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database.connection import Base
from config.security import encrypt_token, decrypt_token

class User(Base):
    """
    Salesperson or Tenant representation.
    Maintains the encrypted Google OAuth 2.0 refresh token and WhatsApp identifier.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    phone_number = Column(String, unique=True, index=True, nullable=False)  # Format: "52155..." or "+1..."
    encrypted_refresh_token = Column(Text, nullable=True)
    
    # Associated Workspace resources
    spreadsheet_id = Column(String, nullable=True)  # CRM Sheet ID
    template_doc_id = Column(String, nullable=True)  # Quote Template Doc ID
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    clients = relationship("Client", back_populates="user", cascade="all, delete-orphan")

    def set_refresh_token(self, raw_token: str):
        """Encrypts and stores the Google OAuth 2.0 refresh token."""
        self.encrypted_refresh_token = encrypt_token(raw_token)

    def get_refresh_token(self) -> str:
        """Decrypts and returns the Google OAuth 2.0 refresh token."""
        return decrypt_token(self.encrypted_refresh_token)


class Client(Base):
    """
    Client or Lead belonging to a salesperson.
    """
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    email = Column(String, nullable=True)
    phone_number = Column(String, nullable=True)
    meta_data = Column(JSON, nullable=True)  # Store custom attributes from Sheets CRM
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    user = relationship("User", back_populates="clients")


class ConversationLog(Base):
    """
    Conversation message history logged for audits and context retrieval.
    """
    __tablename__ = "conversation_logs"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, index=True, nullable=False)  # WhatsApp number of the sender/recipient
    sender = Column(String, nullable=False)  # "user" (salesperson) or "agent"
    message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
