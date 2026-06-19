from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, Date, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database.connection import Base
from config.security import encrypt_token, decrypt_token

class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    company_code = Column(String, unique=True, index=True, nullable=False)
    whatsapp_phone_number_id = Column(String, nullable=True)
    encrypted_whatsapp_token = Column(String, nullable=True)
    global_goals = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    users = relationship("User", back_populates="company")

    def set_whatsapp_token(self, token: str):
        if token:
            self.encrypted_whatsapp_token = encrypt_token(token)
        else:
            self.encrypted_whatsapp_token = None

    def get_whatsapp_token(self) -> str | None:
        if self.encrypted_whatsapp_token:
            return decrypt_token(self.encrypted_whatsapp_token)
        return None

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
    
    # Firebase and Role modifications
    firebase_uid = Column(String, unique=True, index=True, nullable=True)
    role = Column(String, default="vendedor_independiente") # 'admin_empresa', 'vendedor_empresa', 'vendedor_independiente'
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    sales_goals = Column(Text, nullable=True)
    objectives = Column(Text, nullable=True)
    
    encrypted_refresh_token = Column(Text, nullable=True)
    
    # Associated Workspace resources
    spreadsheet_id = Column(String, nullable=True)  # CRM Sheet ID
    template_doc_id = Column(String, nullable=True)  # Quote Template Doc ID
    calendar_id = Column(String, default="primary") # Selected Calendar ID
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    clients = relationship("Client", back_populates="user", cascade="all, delete-orphan")
    company = relationship("Company", back_populates="users")
    accountability_plan = relationship("AccountabilityPlan", back_populates="user", uselist=False, cascade="all, delete-orphan")
    daily_logs = relationship("DailyActivityLog", back_populates="user", cascade="all, delete-orphan")
    calendar_audits = relationship("CalendarEventAudit", back_populates="user", cascade="all, delete-orphan")

    def set_refresh_token(self, raw_token: str):
        """Encrypts and stores the Google OAuth 2.0 refresh token."""
        if raw_token:
            self.encrypted_refresh_token = encrypt_token(raw_token)

    def get_refresh_token(self) -> str:
        """Decrypts and returns the Google OAuth 2.0 refresh token."""
        return decrypt_token(self.encrypted_refresh_token) if self.encrypted_refresh_token else None


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


class AccountabilityPlan(Base):
    __tablename__ = "accountability_plans"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    
    citas_meta_mensual = Column(Integer, default=0)
    citas_meta_semanal = Column(Integer, default=0)
    citas_meta_diaria = Column(Integer, default=0)
    
    llamadas_meta_mensual = Column(Integer, default=0)
    llamadas_meta_semanal = Column(Integer, default=0)
    llamadas_meta_diaria = Column(Integer, default=0)
    
    propuestas_meta_mensual = Column(Integer, default=0)
    propuestas_meta_semanal = Column(Integer, default=0)
    propuestas_meta_diaria = Column(Integer, default=0)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    user = relationship("User", back_populates="accountability_plan")


class DailyActivityLog(Base):
    __tablename__ = "daily_activity_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    date = Column(Date, nullable=False)
    
    citas_completadas = Column(Integer, default=0)
    llamadas_completadas = Column(Integer, default=0)
    propuestas_completadas = Column(Integer, default=0)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    user = relationship("User", back_populates="daily_logs")

    __table_args__ = (
        UniqueConstraint('user_id', 'date', name='uix_user_date'),
    )


class CalendarEventAudit(Base):
    __tablename__ = "calendar_event_audits"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    event_id = Column(String, index=True, nullable=False)
    event_summary = Column(String, nullable=True)
    event_start = Column(DateTime(timezone=True), nullable=True)
    is_completed = Column(Boolean, default=False)
    audit_status = Column(String, default="pending")  # 'pending', 'sent_whatsapp', 'confirmed', 'no_show'
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    user = relationship("User", back_populates="calendar_audits")

    __table_args__ = (
        UniqueConstraint('user_id', 'event_id', name='uix_user_event'),
    )
