"""
Configuration settings for the Telegram Bot
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ─── Bot Settings ────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

# ─── Database ────────────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///bot_database.db")

# ─── Bot Behavior ────────────────────────────────────────────────────────────
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")   # without @
INVITE_ONLY: bool = os.getenv("INVITE_ONLY", "true").lower() == "true"

# ─── Stars Payment ───────────────────────────────────────────────────────────
STARS_PROVIDER_TOKEN: str = os.getenv("STARS_PROVIDER_TOKEN", "")  # empty for XTR

# ─── Scheduler ───────────────────────────────────────────────────────────────
SCHEDULER_TIMEZONE: str = os.getenv("SCHEDULER_TIMEZONE", "Asia/Riyadh")

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
