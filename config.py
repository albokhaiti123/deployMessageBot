"""
Configuration settings for the Telegram Bot.
Static values come from .env; dynamic admin list is merged from the database at runtime.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ─── Bot Settings ────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# Static admins from .env (always have access even if DB is unavailable)
_STATIC_ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

# ─── Database ────────────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///bot_database.db")

# ─── Bot Behavior ────────────────────────────────────────────────────────────
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")   # without @
INVITE_ONLY: bool = os.getenv("INVITE_ONLY", "true").lower() == "true"

# ─── Stars Payment ───────────────────────────────────────────────────────────
# MUST be empty string "" for Telegram Stars (XTR) — do NOT set a provider token
STARS_PROVIDER_TOKEN: str = ""

# ─── Scheduler ───────────────────────────────────────────────────────────────
SCHEDULER_TIMEZONE: str = os.getenv("SCHEDULER_TIMEZONE", "Asia/Riyadh")

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")


# ─── Dynamic admin check (merges .env + DB) ──────────────────────────────────
def is_admin(telegram_id: int) -> bool:
    """
    Returns True if telegram_id is a bot admin.
    Checks static list first (fast), then queries DB for dynamic admins.
    """
    if telegram_id in _STATIC_ADMIN_IDS:
        return True
    try:
        from database.models import get_session
        from database import db
        with get_session() as session:
            dynamic = db.get_dynamic_admin_ids(session)
        return telegram_id in dynamic
    except Exception:
        return False


def get_all_admin_ids() -> list[int]:
    """Returns combined list of static + dynamic admin IDs."""
    try:
        from database.models import get_session
        from database import db
        with get_session() as session:
            dynamic = db.get_dynamic_admin_ids(session)
        return list(set(_STATIC_ADMIN_IDS + dynamic))
    except Exception:
        return list(_STATIC_ADMIN_IDS)


# Keep for backward compat (used in notification loops)
ADMIN_IDS: list[int] = _STATIC_ADMIN_IDS
