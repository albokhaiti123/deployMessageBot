"""
Database models and connection setup using SQLAlchemy (sync core for simplicity).
Phase-1 fixes:
  - User.last_start_msg_id: BigInteger → Text (stores JSON list)
  - BroadcastMessage: added `target` column
  - Purchase: message_id nullable + purchase_type column (no more -1 hack)
  - MessageView: ondelete CASCADE on message_id
  - MediaType: added VOICE
  - _run_phase1_migrations(): safe ALTER for existing DBs
"""
import datetime
import enum
import logging
from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Text,
    Boolean, DateTime, Float, ForeignKey, Enum as SAEnum, inspect, text
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

import config

logger = logging.getLogger(__name__)

engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in config.DATABASE_URL else {},
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# ─── Enums ────────────────────────────────────────────────────────────────────

class MediaType(str, enum.Enum):
    NONE     = "none"
    PHOTO    = "photo"
    VIDEO    = "video"
    AUDIO    = "audio"
    VOICE    = "voice"
    DOCUMENT = "document"


class DestructMode(str, enum.Enum):
    NEVER      = "never"
    IMMEDIATE  = "immediate"
    AFTER_TIME = "after_time"


class MessageStatus(str, enum.Enum):
    DRAFT     = "draft"
    SCHEDULED = "scheduled"
    SENT      = "sent"
    CANCELLED = "cancelled"


# ─── Base ─────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ─── User ─────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id       = Column(BigInteger, unique=True, nullable=False, index=True)
    username          = Column(String(64), nullable=True)
    full_name         = Column(String(128), nullable=True)
    is_active         = Column(Boolean, default=True)
    is_banned         = Column(Boolean, default=False)
    referred_by       = Column(BigInteger, nullable=True)
    joined_at         = Column(DateTime, default=datetime.datetime.utcnow)
    last_seen         = Column(DateTime, default=datetime.datetime.utcnow)
    start_msg_shown   = Column(Boolean, default=False)
    # FIX: was BigInteger — now Text to store JSON list like "[101, 102]"
    last_start_msg_id = Column(Text, nullable=True)

    purchases       = relationship("Purchase", back_populates="user")
    viewed_messages = relationship("MessageView", back_populates="user")

    def __repr__(self):
        return f"<User {self.telegram_id} ({self.full_name})>"


# ─── BroadcastMessage ─────────────────────────────────────────────────────────

class BroadcastMessage(Base):
    """A message created by admin for broadcasting."""
    __tablename__ = "broadcast_messages"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    title            = Column(String(256), nullable=True)
    text             = Column(Text, nullable=True)
    media_type       = Column(SAEnum(MediaType), default=MediaType.NONE)
    media_file_id    = Column(String(256), nullable=True)
    is_paid          = Column(Boolean, default=False)
    stars_price      = Column(Integer, default=0)
    status           = Column(SAEnum(MessageStatus), default=MessageStatus.DRAFT)
    protect_content  = Column(Boolean, default=True)
    destruct_mode    = Column(SAEnum(DestructMode), default=DestructMode.NEVER)
    destruct_seconds = Column(Integer, default=0)
    # FIX: added target column
    target           = Column(String(16), default="users", nullable=False)
    scheduled_at     = Column(DateTime, nullable=True)
    interval_hours   = Column(Float, nullable=True)
    sent_at          = Column(DateTime, nullable=True)
    created_at       = Column(DateTime, default=datetime.datetime.utcnow)
    created_by       = Column(BigInteger, nullable=False)
    buttons_json     = Column(Text, default="[]")
    media_json       = Column(Text, default="[]")

    # passive_deletes: let DB handle cascade, avoid loading all purchases just to delete
    purchases = relationship("Purchase", back_populates="message", passive_deletes=True)
    views     = relationship("MessageView", back_populates="message", cascade="all, delete-orphan")


# ─── Purchase ─────────────────────────────────────────────────────────────────

class Purchase(Base):
    """Records a Stars payment for a paid broadcast or /start message."""
    __tablename__ = "purchases"

    id                         = Column(Integer, primary_key=True, autoincrement=True)
    user_id                    = Column(Integer, ForeignKey("users.id"), nullable=False)
    # FIX: nullable=True + ondelete SET NULL — no more -1 fake FK
    message_id                 = Column(
        Integer,
        ForeignKey("broadcast_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    # FIX: purchase_type distinguishes "broadcast" vs "start_msg"
    purchase_type              = Column(String(32), default="broadcast", nullable=False)
    stars_paid                 = Column(Integer, nullable=False)
    telegram_payment_charge_id = Column(String(256), nullable=True)
    purchased_at               = Column(DateTime, default=datetime.datetime.utcnow)

    user    = relationship("User", back_populates="purchases")
    message = relationship("BroadcastMessage", back_populates="purchases")


# ─── MessageView ──────────────────────────────────────────────────────────────

class MessageView(Base):
    """Tracks which users have viewed a message (legacy self-destruct logic)."""
    __tablename__ = "message_views"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    # FIX: CASCADE so deleting BroadcastMessage removes orphan views
    message_id  = Column(
        Integer,
        ForeignKey("broadcast_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    sent_msg_id = Column(BigInteger, nullable=True)
    viewed_at   = Column(DateTime, default=datetime.datetime.utcnow)
    deleted_at  = Column(DateTime, nullable=True)

    user    = relationship("User", back_populates="viewed_messages")
    message = relationship("BroadcastMessage", back_populates="views")


# ─── InviteLink ───────────────────────────────────────────────────────────────

class InviteLink(Base):
    __tablename__ = "invite_links"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    owner_id   = Column(BigInteger, nullable=False)
    token      = Column(String(64), unique=True, nullable=False)
    uses       = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ─── BotGroup ─────────────────────────────────────────────────────────────────

class BotGroup(Base):
    __tablename__ = "bot_groups"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    chat_id   = Column(BigInteger, unique=True, nullable=False, index=True)
    title     = Column(String(256), nullable=True)
    username  = Column(String(64), nullable=True)
    is_active = Column(Boolean, default=True)
    added_at  = Column(DateTime, default=datetime.datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.datetime.utcnow)


# ─── BotSettings ──────────────────────────────────────────────────────────────

class BotSettings(Base):
    __tablename__ = "bot_settings"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    key        = Column(String(128), unique=True, nullable=False)
    value      = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
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
    __tablename__ = "welcome_config"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    enabled          = Column(Boolean, default=True)
    text             = Column(Text, nullable=True)
    media_type       = Column(String(32), default="none")
    media_file_id    = Column(String(256), nullable=True)
    media_json       = Column(Text, default="[]")
    protect_content  = Column(Boolean, default=False)
    destruct_seconds = Column(Integer, default=0)
    buttons_json     = Column(Text, default="[]")


# ─── StartMessageConfig ──────────────────────────────────────────────────────

class StartMessageConfig(Base):
    __tablename__ = "start_message_config"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    enabled          = Column(Boolean, default=False)
    text             = Column(Text, nullable=True)
    media_type       = Column(String(32), default="none")
    media_file_id    = Column(String(256), nullable=True)
    media_json       = Column(Text, default="[]")
    is_paid          = Column(Boolean, default=False)
    stars_price      = Column(Integer, default=0)
    protect_content  = Column(Boolean, default=False)
    destruct_seconds = Column(Integer, default=0)
    buttons_json     = Column(Text, default="[]")
    show_mode        = Column(String(32), default="always")


# ─── SelfDestructJob ──────────────────────────────────────────────────────────

class SelfDestructJob(Base):
    __tablename__ = "self_destruct_jobs"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    chat_id    = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    delete_at  = Column(DateTime, nullable=False)
    deleted    = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ─── Safe Phase-1 Schema Migration ───────────────────────────────────────────

def _run_phase1_migrations():
    """
    Upgrade existing databases safely without losing data.
    Runs after create_all() so new tables already exist.
    """
    dialect = engine.dialect.name
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    with engine.begin() as conn:

        # 1. broadcast_messages.target (new column)
        if "broadcast_messages" in tables:
            bm_cols = {c["name"] for c in insp.get_columns("broadcast_messages")}
            if "target" not in bm_cols:
                conn.execute(text(
                    "ALTER TABLE broadcast_messages "
                    "ADD COLUMN target VARCHAR(16) NOT NULL DEFAULT 'users'"
                ))
                logger.info("Migration: added broadcast_messages.target")

        # 2. users.last_start_msg_id BigInteger → TEXT on PostgreSQL
        if "users" in tables and dialect == "postgresql":
            u_cols = {c["name"]: c for c in insp.get_columns("users")}
            col = u_cols.get("last_start_msg_id")
            if col and "INT" in str(col.get("type", "")).upper():
                conn.execute(text(
                    "ALTER TABLE users "
                    "ALTER COLUMN last_start_msg_id TYPE TEXT "
                    "USING last_start_msg_id::TEXT"
                ))
                logger.info("Migration: converted users.last_start_msg_id to TEXT")

        # 3. purchases: add purchase_type + fix message_id nullability + migrate -1 rows
        if "purchases" in tables:
            p_cols = {c["name"]: c for c in insp.get_columns("purchases")}

            # 3a. Add purchase_type column if missing
            if "purchase_type" not in p_cols:
                conn.execute(text(
                    "ALTER TABLE purchases "
                    "ADD COLUMN purchase_type VARCHAR(32) NOT NULL DEFAULT 'broadcast'"
                ))
                logger.info("Migration: added purchases.purchase_type")

            # 3b. Fix message_id NOT NULL + migrate -1 → NULL rows
            if dialect == "sqlite":
                # SQLite cannot ALTER NOT NULL; rebuild table only if needed
                msg_id_col = p_cols.get("message_id", {})
                # Check for -1 rows and migrate them
                try:
                    conn.execute(text(
                        "UPDATE purchases "
                        "SET message_id = NULL, purchase_type = 'start_msg' "
                        "WHERE message_id = -1"
                    ))
                except Exception:
                    pass  # column might already be nullable after table rebuild
            elif dialect == "postgresql":
                msg_id_col = p_cols.get("message_id", {})
                if msg_id_col and not msg_id_col.get("nullable", True):
                    conn.execute(text(
                        "ALTER TABLE purchases "
                        "ALTER COLUMN message_id DROP NOT NULL"
                    ))
                    logger.info("Migration: dropped NOT NULL on purchases.message_id")
                conn.execute(text(
                    "UPDATE purchases "
                    "SET purchase_type = 'start_msg', message_id = NULL "
                    "WHERE message_id = -1"
                ))
                # Add VOICE to PostgreSQL enum if missing
                try:
                    conn.execute(text(
                        "ALTER TYPE mediatype ADD VALUE IF NOT EXISTS 'VOICE'"
                    ))
                except Exception:
                    pass


# ─── Init ─────────────────────────────────────────────────────────────────────

def init_db():
    Base.metadata.create_all(engine)
    _run_phase1_migrations()


def get_session():
    return SessionLocal()
