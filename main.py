"""
Bot entry point — builds the Application and starts polling.
Compatible with PM2 (runs as a foreground process).
"""
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from telegram import BotCommand, Update
from telegram.ext import Application, ContextTypes

import config
from database.models import init_db
from bot.handlers.user import register_user_handlers
from bot.handlers.admin import register_admin_handlers
from bot.handlers.payment import register_payment_handlers
from bot.services.scheduler import (
    start_scheduler, stop_scheduler,
    schedule_cleanup, schedule_purge, restore_scheduled_jobs,
)

# ─── Logging (rotated, bounded disk usage) ───────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            "logs/bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger(__name__)


# ─── Global error handler ────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while handling update %s", update, exc_info=context.error)


# ─── Post-init / shutdown hooks ──────────────────────────────────────────────
async def post_init(app: Application) -> None:
    if not config.BOT_USERNAME:
        try:
            me = await app.bot.get_me()
            if me.username:
                config.BOT_USERNAME = me.username
                logger.info(f"Auto-populated BOT_USERNAME=@{config.BOT_USERNAME}")
            else:
                logger.warning("BOT_USERNAME is empty and could not be resolved from Telegram.")
        except Exception as e:
            logger.warning(f"Could not auto-populate BOT_USERNAME: {e}")

    await app.bot.set_my_commands([
        BotCommand("start",  "بدء استخدام البوت"),
        BotCommand("help",   "المساعدة"),
        BotCommand("admin",  "لوحة التحكم (للمشرفين)"),
        BotCommand("cancel", "إلغاء العملية الحالية"),
    ])

    start_scheduler()
    schedule_cleanup(app.bot)
    schedule_purge()
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

    init_db()
    logger.info("Database initialized.")

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(15.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(15.0)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    register_admin_handlers(app)
    register_payment_handlers(app)
    register_user_handlers(app)
    app.add_error_handler(error_handler)

    logger.info("Starting polling...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=[
            "message",
            "callback_query",
            "pre_checkout_query",
            "my_chat_member",
        ],
    )


if __name__ == "__main__":
    main()
