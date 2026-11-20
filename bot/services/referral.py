"""
Referral / invite-link service.
Phase-1 fixes:
  - Separated read-only lookup (get_referrer_by_token) from counter increment (record_invite_use)
  - build_invite_url: validates BOT_USERNAME is set
  - parse_referral_token: strips token safely
"""
import logging
import config
from database.models import get_session
from database import db

logger = logging.getLogger(__name__)


def build_invite_url(token: str) -> str:
    """Build a t.me deep-link from an invite token."""
    username = (config.BOT_USERNAME or "").lstrip("@").strip()
    if not username:
        logger.warning("BOT_USERNAME is empty — generated invite link will be broken.")
    return f"https://t.me/{username}?start=ref_{token}"


def get_or_create_user_invite(owner_telegram_id: int) -> str:
    """Return the first invite token for this owner (admin), creating one if needed."""
    with get_session() as session:
        links = db.get_user_invite_links(session, owner_telegram_id)
        if links:
            return links[0].token
        link = db.create_invite_link(session, owner_telegram_id)
        return link.token


def parse_referral_token(start_param: str) -> str | None:
    """Extract token from 'ref_<token>' start param. Returns None if not a referral."""
    if start_param and start_param.startswith("ref_"):
        token = start_param[4:].strip()
        return token or None
    return None


def get_referrer_by_token(token: str) -> int | None:
    """
    Read-only lookup: return owner telegram_id for a valid token.
    Does NOT increment uses — call record_invite_use() separately after confirming new user.
    """
    if not token:
        return None
    with get_session() as session:
        link = db.get_invite_link_by_token(session, token)
        return link.owner_id if link else None


def record_invite_use(token: str) -> None:
    """
    Increment use count on an invite link.
    Call ONLY after confirming the user is genuinely new and not banned.
    """
    if not token:
        return
    with get_session() as session:
        db.increment_link_uses(session, token)


def resolve_referrer(token: str) -> int | None:
    """
    Backward-compatible helper: lookup + increment in one call.
    Prefer get_referrer_by_token() + record_invite_use() in new code.
    """
    owner_id = get_referrer_by_token(token)
    if owner_id is not None:
        record_invite_use(token)
    return owner_id
