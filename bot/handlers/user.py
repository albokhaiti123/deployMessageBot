"""
User-facing handlers: /start, /help.
- Welcome message is fully configurable by admin from the control panel.
- No invite links exposed to users (admin-only).
- Users only see broadcast messages — no extra buttons or menus.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

import config
from database.models import get_session
from database import db

logger = logging.getLogger(__name__)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_welcome_config(session) -> tuple[bool, str]:
    """Returns (welcome_enabled, welcome_text)."""
    enabled_raw = db.get_setting(session, "welcome_enabled", "true")
    enabled = enabled_raw.lower() == "true"
    text = db.get_setting(
        session,
        "welcome_text",
        "👋 أهلاً <b>{name}</b>!\n\n🎉 لقد انضممت بنجاح.\nسيتم إرسال المحتوى إليك مباشرة هنا.",
    )
    return enabled, text


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    start_param = args[0] if args else ""

    # Resolve referral token (only admin-generated links work)
    referrer_id = None
    if start_param and start_param.startswith("ref_"):
        token = start_param[4:]
        from bot.services.referral import resolve_referrer
        referrer_id = resolve_referrer(token)

    with get_session() as session:
        db_user = db.get_user_by_telegram_id(session, user.id)
        if db_user and db_user.is_banned:
            await update.message.reply_text("🚫 أنت محظور من استخدام هذا البوت.")
            return

        db_user, created = db.get_or_create_user(
            session,
            telegram_id=user.id,
            username=user.username,
            full_name=user.full_name,
            referred_by=referrer_id,
        )

        # Read welcome config
        welcome_enabled, welcome_text = _get_welcome_config(session)

    if created:
        # Notify all admins about new subscriber
        for admin_id in config.get_all_admin_ids():
            try:
                via = f"عبر رابط دعوة (مُحيل: <code>{referrer_id}</code>)" if referrer_id else "مباشر"
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"📥 <b>مشترك جديد!</b>\n"
                        f"👤 الاسم: {user.full_name or '—'}\n"
                        f"🆔 المعرف: <code>{user.id}</code>\n"
                        f"📎 انضم: {via}"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

    # Send welcome message only if enabled
    if welcome_enabled:
        formatted = welcome_text.replace("{name}", user.first_name or "").replace("{id}", str(user.id))
        await update.message.reply_text(formatted, parse_mode="HTML")


# ─── /help ────────────────────────────────────────────────────────────────────

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_session() as session:
        db_user = db.get_user_by_telegram_id(session, user.id)
        if db_user and db_user.is_banned:
            return

    # If user is also admin, show admin hint
    extra = "\n\n🛠️ /admin — لوحة التحكم" if config.is_admin(user.id) else ""
    await update.message.reply_text(
        f"📖 <b>المساعدة</b>\n\n"
        f"/start — بدء استخدام البوت\n"
        f"/help — هذه الرسالة"
        f"{extra}",
        parse_mode="HTML",
    )


# ─── viewed_{view_id} callback (immediate self-destruct after user taps button) ─

async def viewed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🗑️ جارٍ حذف الرسالة...")
    data = query.data  # "viewed_<view_id>"
    try:
        view_id = int(data.split("_")[1])
    except (IndexError, ValueError):
        return

    with get_session() as session:
        from database.models import MessageView
        view = session.query(MessageView).filter_by(id=view_id).first()
        if view and not view.deleted_at:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=view.sent_msg_id,
                )
            except Exception:
                pass
            db.mark_view_deleted(session, view_id)


# ─── Register handlers ────────────────────────────────────────────────────────

def register_user_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(viewed_callback, pattern=r"^viewed_\d+$"))
