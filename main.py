"""
Bot entry point — builds the Application and starts polling.
Compatible with PM2 (runs as a foreground process).
"""
import logging
import sys
from telegram import BotCommand
from telegram.ext import Application

import config
from database.models import init_db
from bot.handlers.user import register_user_handlers
from bot.handlers.admin import register_admin_handlers
from bot.handlers.payment import register_payment_handlers
from bot.services.scheduler import (
    start_scheduler, stop_scheduler,
    schedule_cleanup, restore_scheduled_jobs,
)

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ─── Post-init hook (runs after bot connects) ─────────────────────────────────
async def post_init(app: Application) -> None:
    """Set bot commands and start background jobs."""
    await app.bot.set_my_commands([
        BotCommand("start",  "بدء استخدام البوت"),
        BotCommand("help",   "المساعدة"),
        BotCommand("admin",  "لوحة التحكم (للمشرفين)"),
        BotCommand("cancel", "إلغاء العملية الحالية"),
    ])

    start_scheduler()
    schedule_cleanup(app.bot)
    restore_scheduled_jobs(app.bot)
    logger.info("Bot initialized. Scheduler running.")


async def post_shutdown(app: Application) -> None:
    stop_scheduler()
    logger.info("Bot shutdown complete.")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    if not config.BOT_TOKEN:
        logger.critical("BOT_TOKEN is not set! Copy .env.example to .env and fill in your values.")
        sys.exit(1)

    if not config.ADMIN_IDS:
        logger.warning("No ADMIN_IDS set — admin panel will be inaccessible!")

    # Init database
    init_db()
    logger.info("Database initialized.")

    # Build application
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register handlers (order matters: admin before user for /start priority)
    register_admin_handlers(app)
    register_payment_handlers(app)
    register_user_handlers(app)

    logger.info("Starting polling...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "pre_checkout_query"],
    )


if __name__ == "__main__":
    main()
