"""
Database models and connection setup using SQLAlchemy (sync core for simplicity).
"""
import datetime
from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Text,
    Boolean, DateTime, Float, ForeignKey, Enum as SAEnum, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
import enum

import config

engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in config.DATABASE_URL else {},
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# ─── Enums ────────────────────────────────────────────────────────────────────

class MediaType(str, enum.Enum):
    NONE    = "none"
    PHOTO   = "photo"
    VIDEO   = "video"
    AUDIO   = "audio"
    DOCUMENT= "document"


class DestructMode(str, enum.Enum):
    NEVER       = "never"       # لا تُحذف
    IMMEDIATE   = "immediate"   # فور المشاهدة
    AFTER_TIME  = "after_time"  # بعد فترة زمنية


class MessageStatus(str, enum.Enum):
    DRAFT       = "draft"
    SCHEDULED   = "scheduled"
    SENT        = "sent"
    CANCELLED   = "cancelled"


# ─── Base ─────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ─── User ─────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id     = Column(BigInteger, unique=True, nullable=False, index=True)
    username        = Column(String(64), nullable=True)
    full_name       = Column(String(128), nullable=True)
    is_active       = Column(Boolean, default=True)
    is_banned       = Column(Boolean, default=False)
    referred_by     = Column(BigInteger, nullable=True)
    joined_at       = Column(DateTime, default=datetime.datetime.utcnow)
    last_seen       = Column(DateTime, default=datetime.datetime.utcnow)
    start_msg_shown = Column(Boolean, default=False)   # for "once_after_join" mode
    last_start_msg_id = Column(BigInteger, nullable=True)  # telegram msg_id of last /start reply

    purchases       = relationship("Purchase", back_populates="user")
    viewed_messages = relationship("MessageView", back_populates="user")

    def __repr__(self):
        return f"<User {self.telegram_id} ({self.full_name})>"


# ─── BroadcastMessage ─────────────────────────────────────────────────────────

class BroadcastMessage(Base):
    """A message created by admin for broadcasting."""
    __tablename__ = "broadcast_messages"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    title           = Column(String(256), nullable=True)
    text            = Column(Text, nullable=True)
    media_type      = Column(SAEnum(MediaType), default=MediaType.NONE)
    media_file_id   = Column(String(256), nullable=True)
    is_paid         = Column(Boolean, default=False)
    stars_price     = Column(Integer, default=0)
    status          = Column(SAEnum(MessageStatus), default=MessageStatus.DRAFT)
    protect_content = Column(Boolean, default=True)
    destruct_mode   = Column(SAEnum(DestructMode), default=DestructMode.NEVER)
    destruct_seconds= Column(Integer, default=0)
    scheduled_at    = Column(DateTime, nullable=True)
    interval_hours  = Column(Float, nullable=True)
    sent_at         = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)
    created_by      = Column(BigInteger, nullable=False)
    buttons_json    = Column(Text, default="[]")       # [{"text":"...", "url":"..."}]
    media_json      = Column(Text, default="[]")       # [{"type":"photo","file_id":"..."}]

    purchases       = relationship("Purchase", back_populates="message")
    views           = relationship("MessageView", back_populates="message")


# ─── Purchase ─────────────────────────────────────────────────────────────────

class Purchase(Base):
    """Records a Stars payment for a paid message."""
    __tablename__ = "purchases"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    message_id  = Column(Integer, ForeignKey("broadcast_messages.id"), nullable=False)
    stars_paid  = Column(Integer, nullable=False)
    telegram_payment_charge_id = Column(String(256), nullable=True)
    purchased_at= Column(DateTime, default=datetime.datetime.utcnow)

    user        = relationship("User", back_populates="purchases")
    message     = relationship("BroadcastMessage", back_populates="purchases")


# ─── MessageView ──────────────────────────────────────────────────────────────

class MessageView(Base):
    """Tracks which users have viewed a message (for self-destruct logic)."""
    __tablename__ = "message_views"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    message_id  = Column(Integer, ForeignKey("broadcast_messages.id"), nullable=False)
    sent_msg_id = Column(BigInteger, nullable=True)
    viewed_at   = Column(DateTime, default=datetime.datetime.utcnow)
    deleted_at  = Column(DateTime, nullable=True)

    user        = relationship("User", back_populates="viewed_messages")
    message     = relationship("BroadcastMessage", back_populates="views")


# ─── InviteLink ───────────────────────────────────────────────────────────────

class InviteLink(Base):
    __tablename__ = "invite_links"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    owner_id    = Column(BigInteger, nullable=False)
    token       = Column(String(64), unique=True, nullable=False)
    uses        = Column(Integer, default=0)
    created_at  = Column(DateTime, default=datetime.datetime.utcnow)


# ─── BotGroup ─────────────────────────────────────────────────────────────────

class BotGroup(Base):
    __tablename__ = "bot_groups"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    chat_id     = Column(BigInteger, unique=True, nullable=False, index=True)
    title       = Column(String(256), nullable=True)
    username    = Column(String(64), nullable=True)
    is_active   = Column(Boolean, default=True)
    added_at    = Column(DateTime, default=datetime.datetime.utcnow)
    last_seen   = Column(DateTime, default=datetime.datetime.utcnow)


# ─── BotSettings ──────────────────────────────────────────────────────────────

class BotSettings(Base):
    """Key-value store for legacy/simple settings."""
    __tablename__ = "bot_settings"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    key         = Column(String(128), unique=True, nullable=False)
    value       = Column(Text, nullable=True)
    updated_at  = Column(DateTime, default=datetime.datetime.utcnow,
                         onupdate=datetime.datetime.utcnow)


# ─── AdminUser ────────────────────────────────────────────────────────────────

class AdminUser(Base):
    __tablename__ = "admin_users"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    added_by    = Column(BigInteger, nullable=False)
    added_at    = Column(DateTime, default=datetime.datetime.utcnow)
    note        = Column(String(256), nullable=True)


# ─── WelcomeConfig ────────────────────────────────────────────────────────────

class WelcomeConfig(Base):
    """Full configuration for the first-time welcome message."""
    __tablename__ = "welcome_config"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    enabled         = Column(Boolean, default=True)
    text            = Column(Text, nullable=True)
    media_type      = Column(String(32), default="none")       # legacy single
    media_file_id   = Column(String(256), nullable=True)        # legacy single
    media_json      = Column(Text, default="[]")                # [{"type":"photo","file_id":"..."}]
    protect_content = Column(Boolean, default=False)
    destruct_seconds= Column(Integer, default=0)
    buttons_json    = Column(Text, default="[]")


# ─── StartMessageConfig ──────────────────────────────────────────────────────

class StartMessageConfig(Base):
    """Configuration for the /start message shown to returning users."""
    __tablename__ = "start_message_config"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    enabled         = Column(Boolean, default=False)
    text            = Column(Text, nullable=True)
    media_type      = Column(String(32), default="none")       # legacy single
    media_file_id   = Column(String(256), nullable=True)        # legacy single
    media_json      = Column(Text, default="[]")                # [{"type":"photo","file_id":"..."}]
    is_paid         = Column(Boolean, default=False)
    stars_price     = Column(Integer, default=0)
    protect_content = Column(Boolean, default=False)
    destruct_seconds= Column(Integer, default=0)
    buttons_json    = Column(Text, default="[]")
    show_mode       = Column(String(32), default="always")  # "always" | "once_after_join"


# ─── SelfDestructJob ──────────────────────────────────────────────────────────

class SelfDestructJob(Base):
    """Tracks messages that need auto-deletion (welcome, start, broadcast)."""
    __tablename__ = "self_destruct_jobs"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    chat_id     = Column(BigInteger, nullable=False)
    message_id  = Column(BigInteger, nullable=False)       # Telegram message_id
    delete_at   = Column(DateTime, nullable=False)
    deleted     = Column(Boolean, default=False)
    created_at  = Column(DateTime, default=datetime.datetime.utcnow)


# ─── Init ─────────────────────────────────────────────────────────────────────

def init_db():
    Base.metadata.create_all(engine)


def get_session():
    return SessionLocal()
