"""
Database helper functions (CRUD operations).
"""
import datetime
import secrets
from typing import Optional
from sqlalchemy.orm import Session

from database.models import (
    User, BroadcastMessage, Purchase, MessageView, InviteLink,
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
    """Returns (user, created). Updates username/full_name on each call."""
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
    """Get delivered messages that need auto-deletion (destruct_mode=AFTER_TIME only)."""
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
    """Insert or update a group record (called when bot joins a group)."""
    import datetime
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
    """Mark group as inactive (called when bot is removed from a group)."""
    from database.models import BotGroup
    row = session.query(BotGroup).filter_by(chat_id=chat_id).first()
    if row:
        row.is_active = False
        session.commit()


def get_active_groups(session: Session):
    """Return list of active groups as dicts."""
    from database.models import BotGroup
    rows = session.query(BotGroup).filter_by(is_active=True).all()
    return [{"chat_id": r.chat_id, "title": r.title or str(r.chat_id)} for r in rows]


def get_group_count(session: Session) -> int:
    from database.models import BotGroup
    return session.query(BotGroup).filter_by(is_active=True).count()
