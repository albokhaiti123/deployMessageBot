"""
Referral / invite-link service.
"""
import config
from database.models import get_session
from database import db


def build_invite_url(token: str) -> str:
    """Build a t.me deep-link from an invite token."""
    return f"https://t.me/{config.BOT_USERNAME}?start=ref_{token}"


def get_or_create_user_invite(owner_telegram_id: int) -> str:
    """Return the first invite token for this user, creating one if needed."""
    with get_session() as session:
        links = db.get_user_invite_links(session, owner_telegram_id)
        if links:
            return links[0].token
        link = db.create_invite_link(session, owner_telegram_id)
        return link.token


def parse_referral_token(start_param: str) -> str | None:
    """Extract token from 'ref_<token>' start param. Returns None if not a referral."""
    if start_param and start_param.startswith("ref_"):
        return start_param[4:]
    return None


def resolve_referrer(token: str) -> int | None:
    """Return the telegram_id of the invite link owner, or None."""
    with get_session() as session:
        link = db.get_invite_link_by_token(session, token)
        if link:
            db.increment_link_uses(session, token)
            return link.owner_id
    return None
