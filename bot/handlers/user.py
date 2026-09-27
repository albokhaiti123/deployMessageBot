"""
User-facing handlers: /start, /help only.
Users have zero interaction beyond receiving broadcast messages.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

import config
from database.models import get_session
from database import db

logger = logging.getLogger(__name__)


def _get_welcome_config(session) -> tuple[bool, str]:
    enabled_raw = db.get_setting(session, "welcome_enabled", "true")
    enabled = enabled_raw.lower() == "true"
    text = db.get_setting(
        session,
        "welcome_text",
        "👋 أهلاً <b>{name}</b>!\n\n🎉 لقد انضممت بنجاح.\nستصلك الرسائل الحصرية هنا مباشرة.",
    )
    return enabled, text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    start_param = args[0] if args else ""

    # Resolve referral (admin-generated links only)
    referrer_id = None
    if start_param and start_param.startswith("ref_"):
        from bot.services.referral import resolve_referrer
        referrer_id = resolve_referrer(start_param[4:])

    with get_session() as session:
        existing = db.get_user_by_telegram_id(session, user.id)
        if existing and existing.is_banned:
            await update.message.reply_text("🚫 أنت محظور من استخدام هذا البوت.")
            return

        db_user, created = db.get_or_create_user(
            session,
            telegram_id=user.id,
            username=user.username,
            full_name=user.full_name,
            referred_by=referrer_id,
        )
        welcome_enabled, welcome_text = _get_welcome_config(session)

    if created:
        for admin_id in config.get_all_admin_ids():
            try:
                via = f"رابط دعوة (مُحيل: <code>{referrer_id}</code>)" if referrer_id else "مباشر"
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"📥 <b>مشترك جديد!</b>\n"
                        f"👤 {user.full_name or '—'}\n"
                        f"🆔 <code>{user.id}</code>\n"
                        f"📎 {via}"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

    if welcome_enabled:
        formatted = (
            welcome_text
            .replace("{name}", user.first_name or "")
            .replace("{id}", str(user.id))
        )
        # No buttons, no inline keyboard — pure text/HTML only
        await update.message.reply_text(formatted, parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_session() as session:
        db_user = db.get_user_by_telegram_id(session, user.id)
        if db_user and db_user.is_banned:
            return

    extra = "\n\n🛠️ /admin — لوحة التحكم" if config.is_admin(user.id) else ""
    await update.message.reply_text(
        f"📖 <b>المساعدة</b>\n\n/start — تشغيل البوت\n/help — هذه الرسالة{extra}",
        parse_mode="HTML",
    )


def register_user_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    # NOTE: No callback handlers — users receive messages only, no interaction
