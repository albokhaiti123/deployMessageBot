"""
User-facing handlers: /start, /help.
Also handles group join/leave events to track active groups.
"""
import logging
from telegram import Update
from telegram.ext import (
    ContextTypes, CommandHandler, ChatMemberHandler
)

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


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    start_param = args[0] if args else ""

    referrer_id = None
    if start_param.startswith("ref_"):
        from bot.services.referral import resolve_referrer
        referrer_id = resolve_referrer(start_param[4:])

    with get_session() as session:
        existing = db.get_user_by_telegram_id(session, user.id)
        if existing and existing.is_banned:
            await update.message.reply_text("🚫 أنت محظور من استخدام هذا البوت.")
            return

        _, created = db.get_or_create_user(
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
        await update.message.reply_text(formatted, parse_mode="HTML")


# ─── /help ────────────────────────────────────────────────────────────────────

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


# ─── Group join/leave tracking ────────────────────────────────────────────────

async def track_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Called when the bot's membership status changes in any chat.
    Tracks groups the bot is added to or removed from.
    """
    result = update.my_chat_member
    if not result:
        return

    chat = result.chat
    if chat.type not in ("group", "supergroup"):
        return

    new_status = result.new_chat_member.status

    with get_session() as session:
        if new_status in ("member", "administrator"):
            db.upsert_group(
                session,
                chat_id=chat.id,
                title=chat.title or "",
                username=getattr(chat, "username", None),
            )
            logger.info(f"Bot joined group: {chat.title} ({chat.id})")

            # Notify admins
            for admin_id in config.get_all_admin_ids():
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"➕ <b>البوت أُضيف لمجموعة!</b>\n"
                            f"📌 {chat.title}\n"
                            f"🆔 <code>{chat.id}</code>"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        elif new_status in ("left", "kicked", "banned"):
            db.deactivate_group(session, chat.id)
            logger.info(f"Bot removed from group: {chat.title} ({chat.id})")

            for admin_id in config.get_all_admin_ids():
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"➖ <b>البوت أُزيل من مجموعة!</b>\n"
                            f"📌 {chat.title}\n"
                            f"🆔 <code>{chat.id}</code>"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass


# ─── Register ─────────────────────────────────────────────────────────────────

def register_user_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  help_cmd))
    # Track when bot is added to / removed from groups
    app.add_handler(ChatMemberHandler(track_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
