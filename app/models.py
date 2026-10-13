from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(String(64), index=True, nullable=False)
    username = Column(String(255), nullable=True)
    name = Column(String(255), nullable=True)
    phone = Column(String(64), nullable=True)
    needs = Column(Text, nullable=True)
    status = Column(String(32), default="new")
    source = Column(String(64), default="telegram", nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    chat_messages = relationship("ChatMessage", back_populates="lead", cascade="all, delete-orphan")
    call_records = relationship("CallRecord", back_populates="lead", cascade="all, delete-orphan")


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    role = Column(String(32), nullable=False)  # user, assistant, system
    content = Column(Text, nullable=False)
    source = Column(String(32), default="text")  # text, voice, call_feedback
    created_at = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead", back_populates="chat_messages")


class CallRecord(Base):
    __tablename__ = "call_records"
    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    provider = Column(String(32), default="zadarma")
    status = Column(String(32), default="initiated")
    call_id = Column(String(128), nullable=True)
    transcript = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lead = relationship("Lead", back_populates="call_records")


class LearningProfile(Base):
    __tablename__ = "learning_profiles"
    id = Column(Integer, primary_key=True, index=True)
    niche = Column(String(255), default="default", unique=True)
    learned_context = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LearningReport(Base):
    __tablename__ = "learning_reports"
    id = Column(Integer, primary_key=True, index=True)
    niche = Column(String(255), default="default", index=True)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    chats_count = Column(Integer, default=0)
    calls_count = Column(Integer, default=0)
    change_summary = Column(Text, default="")
    previous_context = Column(Text, default="")
    updated_context = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class AdminUser(Base):
    __tablename__ = "admin_users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(32), default="manager")  # owner, manager
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("admin_users.id"), nullable=False, index=True)
    token = Column(String(255), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class AdminProfile(Base):
    __tablename__ = "admin_profiles"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("admin_users.id"), unique=True, nullable=False, index=True)
    display_name = Column(String(255), default="")
    avatar_url = Column(String(1024), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
