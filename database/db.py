"""
Database helper functions (CRUD operations).
"""
import json
import datetime
import secrets
from typing import Optional
from sqlalchemy.orm import Session

from database.models import (
    User, BroadcastMessage, Purchase, MessageView, InviteLink,
    WelcomeConfig, StartMessageConfig, SelfDestructJob,
    MediaType, DestructMode, MessageStatus, get_session
)


# ─── User CRUD ────────────────────────────────────────────────────────────────

def get_or_create_user(
    session: Session,
    telegram_id: int,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
    referred_by: Optional[int] = None,
) -> tuple[User, bool]:
    user = session.query(User).filter_by(telegram_id=telegram_id).first()
    created = False
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
            referred_by=referred_by,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        created = True
    else:
        user.username = username
        user.full_name = full_name
        user.last_seen = datetime.datetime.utcnow()
        if not user.referred_by and referred_by:
            user.referred_by = referred_by
        session.commit()
    return user, created


def get_user_by_telegram_id(session: Session, telegram_id: int) -> Optional[User]:
    return session.query(User).filter_by(telegram_id=telegram_id).first()


def get_all_active_users(session: Session) -> list[User]:
    return session.query(User).filter_by(is_active=True, is_banned=False).all()


def get_user_count(session: Session) -> int:
    return session.query(User).filter_by(is_active=True, is_banned=False).count()


def ban_user(session: Session, telegram_id: int) -> bool:
    user = get_user_by_telegram_id(session, telegram_id)
    if user:
        user.is_banned = True
        session.commit()
        return True
    return False


def unban_user(session: Session, telegram_id: int) -> bool:
    user = get_user_by_telegram_id(session, telegram_id)
    if user:
        user.is_banned = False
        session.commit()
        return True
    return False


def get_referral_count(session: Session, telegram_id: int) -> int:
    return session.query(User).filter_by(referred_by=telegram_id).count()


def mark_start_msg_shown(session: Session, telegram_id: int):
    user = get_user_by_telegram_id(session, telegram_id)
    if user:
        user.start_msg_shown = True
        session.commit()


def save_last_start_msg_ids(session: Session, telegram_id: int, msg_ids: list[int]):
    """Store all telegram message_ids of the last /start reply for this user."""
    user = get_user_by_telegram_id(session, telegram_id)
    if user:
        user.last_start_msg_id = json.dumps(msg_ids)
        session.commit()


def get_last_start_msg_ids(session: Session, telegram_id: int) -> list[int]:
    """Get stored last /start message_ids as a list."""
    user = get_user_by_telegram_id(session, telegram_id)
    if not user or not user.last_start_msg_id:
        return []
    raw = user.last_start_msg_id
    # Handle both old single-int and new JSON list formats
    if isinstance(raw, int):
        return [raw]
    try:
        val = json.loads(str(raw))
        return val if isinstance(val, list) else [int(val)]
    except (json.JSONDecodeError, ValueError):
        try:
            return [int(raw)]
        except (ValueError, TypeError):
            return []


def cancel_destruct_for_msg(session: Session, chat_id: int, message_id: int):
    """Mark any pending SelfDestructJob for this specific message as done."""
    from database.models import SelfDestructJob
    rows = (
        session.query(SelfDestructJob)
        .filter_by(chat_id=chat_id, message_id=message_id, deleted=False)
        .all()
    )
    for row in rows:
        row.deleted = True
    if rows:
        session.commit()


# ─── Invite Link CRUD ─────────────────────────────────────────────────────────

def create_invite_link(session: Session, owner_id: int) -> InviteLink:
    token = secrets.token_urlsafe(8)
    link = InviteLink(owner_id=owner_id, token=token)
    session.add(link)
    session.commit()
    session.refresh(link)
    return link


def get_invite_link_by_token(session: Session, token: str) -> Optional[InviteLink]:
    return session.query(InviteLink).filter_by(token=token).first()


def increment_link_uses(session: Session, token: str):
    link = get_invite_link_by_token(session, token)
    if link:
        link.uses += 1
        session.commit()


def get_user_invite_links(session: Session, owner_id: int) -> list[InviteLink]:
    return session.query(InviteLink).filter_by(owner_id=owner_id).all()


# ─── BroadcastMessage CRUD ───────────────────────────────────────────────────

def create_broadcast_message(session: Session, **kwargs) -> BroadcastMessage:
    msg = BroadcastMessage(**kwargs)
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return msg


def get_message_by_id(session: Session, msg_id: int) -> Optional[BroadcastMessage]:
    return session.query(BroadcastMessage).filter_by(id=msg_id).first()


def get_all_messages(session: Session, status: Optional[MessageStatus] = None) -> list[BroadcastMessage]:
    q = session.query(BroadcastMessage)
    if status:
        q = q.filter_by(status=status)
    return q.order_by(BroadcastMessage.created_at.desc()).all()


def update_message_status(session: Session, msg_id: int, status: MessageStatus):
    msg = get_message_by_id(session, msg_id)
    if msg:
        msg.status = status
        if status == MessageStatus.SENT:
            msg.sent_at = datetime.datetime.utcnow()
        session.commit()


def delete_message_record(session: Session, msg_id: int) -> bool:
    msg = get_message_by_id(session, msg_id)
    if msg:
        session.delete(msg)
        session.commit()
        return True
    return False


# ─── Purchase CRUD ───────────────────────────────────────────────────────────

def record_purchase(
    session: Session,
    user_db_id: int,
    message_id: int,
    stars_paid: int,
    charge_id: Optional[str] = None,
) -> Purchase:
    purchase = Purchase(
        user_id=user_db_id,
        message_id=message_id,
        stars_paid=stars_paid,
        telegram_payment_charge_id=charge_id,
    )
    session.add(purchase)
    session.commit()
    session.refresh(purchase)
    return purchase


def user_has_purchased(session: Session, user_db_id: int, message_id: int) -> bool:
    return (
        session.query(Purchase)
        .filter_by(user_id=user_db_id, message_id=message_id)
        .first()
        is not None
    )


def get_total_stars_earned(session: Session) -> int:
    result = session.query(Purchase).all()
    return sum(p.stars_paid for p in result)


# ─── MessageView CRUD ─────────────────────────────────────────────────────────

def record_view(
    session: Session,
    user_db_id: int,
    message_id: int,
    sent_msg_id: Optional[int] = None,
) -> MessageView:
    view = MessageView(
        user_id=user_db_id,
        message_id=message_id,
        sent_msg_id=sent_msg_id,
    )
    session.add(view)
    session.commit()
    session.refresh(view)
    return view


def get_pending_deletions(session: Session, now: datetime.datetime) -> list[MessageView]:
    from database.models import BroadcastMessage, DestructMode
    return (
        session.query(MessageView)
        .join(BroadcastMessage)
        .filter(
            MessageView.deleted_at.is_(None),
            MessageView.sent_msg_id.isnot(None),
            BroadcastMessage.destruct_mode == DestructMode.AFTER_TIME,
            BroadcastMessage.destruct_seconds > 0,
        )
        .all()
    )


def mark_view_deleted(session: Session, view_id: int):
    view = session.query(MessageView).filter_by(id=view_id).first()
    if view:
        view.deleted_at = datetime.datetime.utcnow()
        session.commit()


# ─── BotSettings CRUD ─────────────────────────────────────────────────────────

def get_setting(session: Session, key: str, default: Optional[str] = None) -> Optional[str]:
    from database.models import BotSettings
    row = session.query(BotSettings).filter_by(key=key).first()
    return row.value if row else default


def set_setting(session: Session, key: str, value: str):
    from database.models import BotSettings
    row = session.query(BotSettings).filter_by(key=key).first()
    if row:
        row.value = value
    else:
        row = BotSettings(key=key, value=value)
        session.add(row)
    session.commit()


# ─── AdminUser CRUD ───────────────────────────────────────────────────────────

def get_dynamic_admin_ids(session: Session) -> list[int]:
    from database.models import AdminUser
    return [a.telegram_id for a in session.query(AdminUser).all()]


def add_admin(session: Session, telegram_id: int, added_by: int, note: str = "") -> bool:
    from database.models import AdminUser
    existing = session.query(AdminUser).filter_by(telegram_id=telegram_id).first()
    if existing:
        return False
    session.add(AdminUser(telegram_id=telegram_id, added_by=added_by, note=note))
    session.commit()
    return True


def remove_admin(session: Session, telegram_id: int) -> bool:
    from database.models import AdminUser
    row = session.query(AdminUser).filter_by(telegram_id=telegram_id).first()
    if row:
        session.delete(row)
        session.commit()
        return True
    return False


def list_admins(session: Session):
    from database.models import AdminUser
    return session.query(AdminUser).all()


# ─── BotGroup CRUD ────────────────────────────────────────────────────────────

def upsert_group(session: Session, chat_id: int, title: str, username: Optional[str] = None):
    from database.models import BotGroup
    row = session.query(BotGroup).filter_by(chat_id=chat_id).first()
    if row:
        row.title     = title
        row.username  = username
        row.is_active = True
        row.last_seen = datetime.datetime.utcnow()
    else:
        row = BotGroup(chat_id=chat_id, title=title, username=username)
        session.add(row)
    session.commit()


def deactivate_group(session: Session, chat_id: int):
    from database.models import BotGroup
    row = session.query(BotGroup).filter_by(chat_id=chat_id).first()
    if row:
        row.is_active = False
        session.commit()


def get_active_groups(session: Session):
    from database.models import BotGroup
    rows = session.query(BotGroup).filter_by(is_active=True).all()
    return [{"chat_id": r.chat_id, "title": r.title or str(r.chat_id)} for r in rows]


def get_group_count(session: Session) -> int:
    from database.models import BotGroup
    return session.query(BotGroup).filter_by(is_active=True).count()


# ─── WelcomeConfig CRUD ──────────────────────────────────────────────────────

def get_welcome_config(session: Session) -> dict:
    """Return welcome config as a plain dict (safe outside session)."""
    row = session.query(WelcomeConfig).first()
    if not row:
        return {
            "enabled": True,
            "text": "👋 أهلاً <b>{name}</b>!\n\n🎉 لقد انضممت بنجاح.\nستصلك الرسائل الحصرية هنا مباشرة.",
            "media_type": "none",
            "media_file_id": None,
            "protect_content": False,
            "destruct_seconds": 0,
            "buttons": [],
        }
    return {
        "enabled": row.enabled,
        "text": row.text,
        "media_type": row.media_type or "none",
        "media_file_id": row.media_file_id,
        "protect_content": row.protect_content,
        "destruct_seconds": row.destruct_seconds or 0,
        "buttons": json.loads(row.buttons_json or "[]"),
    }


def save_welcome_config(session: Session, **kwargs):
    """Upsert welcome config. Pass only fields you want to update."""
    row = session.query(WelcomeConfig).first()
    if not row:
        row = WelcomeConfig()
        session.add(row)
    for key, val in kwargs.items():
        if key == "buttons":
            row.buttons_json = json.dumps(val, ensure_ascii=False)
        elif hasattr(row, key):
            setattr(row, key, val)
    session.commit()


# ─── StartMessageConfig CRUD ─────────────────────────────────────────────────

def get_start_msg_config(session: Session) -> dict:
    """Return start-message config as a plain dict."""
    row = session.query(StartMessageConfig).first()
    if not row:
        return {
            "enabled": False,
            "text": None,
            "media_type": "none",
            "media_file_id": None,
            "media_list": [],
            "is_paid": False,
            "stars_price": 0,
            "protect_content": False,
            "destruct_seconds": 0,
            "buttons": [],
            "show_mode": "always",
        }
    media_list = json.loads(row.media_json or "[]")
    # Backward compat: if media_list is empty but legacy single exists
    if not media_list and row.media_file_id and row.media_type and row.media_type != "none":
        media_list = [{"type": row.media_type, "file_id": row.media_file_id}]
    return {
        "enabled": row.enabled,
        "text": row.text,
        "media_type": row.media_type or "none",
        "media_file_id": row.media_file_id,
        "media_list": media_list,
        "is_paid": row.is_paid,
        "stars_price": row.stars_price or 0,
        "protect_content": row.protect_content,
        "destruct_seconds": row.destruct_seconds or 0,
        "buttons": json.loads(row.buttons_json or "[]"),
        "show_mode": row.show_mode or "always",
    }


def save_start_msg_config(session: Session, **kwargs):
    """Upsert start-message config."""
    row = session.query(StartMessageConfig).first()
    if not row:
        row = StartMessageConfig()
        session.add(row)
    for key, val in kwargs.items():
        if key == "buttons":
            row.buttons_json = json.dumps(val, ensure_ascii=False)
        elif key == "media_list":
            row.media_json = json.dumps(val, ensure_ascii=False)
            # Also set legacy fields from first item
            if val:
                row.media_type = val[0].get("type", "none")
                row.media_file_id = val[0].get("file_id")
            else:
                row.media_type = "none"
                row.media_file_id = None
        elif hasattr(row, key):
            setattr(row, key, val)
    session.commit()


# ─── SelfDestructJob CRUD ────────────────────────────────────────────────────

def schedule_destruct(session: Session, chat_id: int, message_id: int, seconds: int):
    """Register a message for auto-deletion after `seconds`."""
    if seconds <= 0:
        return
    delete_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
    job = SelfDestructJob(chat_id=chat_id, message_id=message_id, delete_at=delete_at)
    session.add(job)
    session.commit()


def get_due_destruct_jobs(session: Session, now: datetime.datetime) -> list[dict]:
    """Return list of dicts for messages that need deletion now."""
    rows = (
        session.query(SelfDestructJob)
        .filter(
            SelfDestructJob.deleted == False,
            SelfDestructJob.delete_at <= now,
        )
        .all()
    )
    result = [{"id": r.id, "chat_id": r.chat_id, "message_id": r.message_id} for r in rows]
    return result


def mark_destruct_done(session: Session, job_id: int):
    row = session.query(SelfDestructJob).filter_by(id=job_id).first()
    if row:
        row.deleted = True
        session.commit()
