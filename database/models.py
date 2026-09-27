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


# ─── Models ───────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id     = Column(BigInteger, unique=True, nullable=False, index=True)
    username        = Column(String(64), nullable=True)
    full_name       = Column(String(128), nullable=True)
    is_active       = Column(Boolean, default=True)
    is_banned       = Column(Boolean, default=False)
    referred_by     = Column(BigInteger, nullable=True)   # telegram_id of referrer
    joined_at       = Column(DateTime, default=datetime.datetime.utcnow)
    last_seen       = Column(DateTime, default=datetime.datetime.utcnow)

    purchases       = relationship("Purchase", back_populates="user")
    viewed_messages = relationship("MessageView", back_populates="user")

    def __repr__(self):
        return f"<User {self.telegram_id} ({self.full_name})>"


class BroadcastMessage(Base):
    """A message created by admin for broadcasting."""
    __tablename__ = "broadcast_messages"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    title           = Column(String(256), nullable=True)          # internal label
    text            = Column(Text, nullable=True)
    media_type      = Column(SAEnum(MediaType), default=MediaType.NONE)
    media_file_id   = Column(String(256), nullable=True)          # Telegram file_id
    is_paid         = Column(Boolean, default=False)
    stars_price     = Column(Integer, default=0)                  # price in XTR stars
    status          = Column(SAEnum(MessageStatus), default=MessageStatus.DRAFT)
    protect_content = Column(Boolean, default=True)               # disable forward/copy
    destruct_mode   = Column(SAEnum(DestructMode), default=DestructMode.NEVER)
    destruct_seconds= Column(Integer, default=0)                  # seconds after view
    scheduled_at    = Column(DateTime, nullable=True)             # None = send now
    interval_hours  = Column(Float, nullable=True)                # repeat every N hours
    sent_at         = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)
    created_by      = Column(BigInteger, nullable=False)          # admin telegram_id

    purchases       = relationship("Purchase", back_populates="message")
    views           = relationship("MessageView", back_populates="message")

    def __repr__(self):
        return f"<BroadcastMessage id={self.id} paid={self.is_paid}>"


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


class MessageView(Base):
    """Tracks which users have viewed a message (for self-destruct logic)."""
    __tablename__ = "message_views"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    message_id  = Column(Integer, ForeignKey("broadcast_messages.id"), nullable=False)
    sent_msg_id = Column(BigInteger, nullable=True)   # Telegram message id in user chat
    viewed_at   = Column(DateTime, default=datetime.datetime.utcnow)
    deleted_at  = Column(DateTime, nullable=True)

    user        = relationship("User", back_populates="viewed_messages")
    message     = relationship("BroadcastMessage", back_populates="views")


class InviteLink(Base):
    """Custom invite links per user (for tracking referrals)."""
    __tablename__ = "invite_links"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    owner_id    = Column(BigInteger, nullable=False)   # telegram_id of link owner
    token       = Column(String(64), unique=True, nullable=False)
    uses        = Column(Integer, default=0)
    created_at  = Column(DateTime, default=datetime.datetime.utcnow)


class BotSettings(Base):
    """Key-value store for bot-wide settings (welcome message, etc.)."""
    __tablename__ = "bot_settings"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    key         = Column(String(128), unique=True, nullable=False)
    value       = Column(Text, nullable=True)
    updated_at  = Column(DateTime, default=datetime.datetime.utcnow,
                         onupdate=datetime.datetime.utcnow)


class AdminUser(Base):
    """Dynamic admin list (supplements the static ADMIN_IDS from .env)."""
    __tablename__ = "admin_users"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    added_by    = Column(BigInteger, nullable=False)
    added_at    = Column(DateTime, default=datetime.datetime.utcnow)
    note        = Column(String(256), nullable=True)


# ─── Init ─────────────────────────────────────────────────────────────────────

def init_db():
    Base.metadata.create_all(engine)


def get_session():
    return SessionLocal()

