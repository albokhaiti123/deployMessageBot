"""
User-facing handlers: /start, /help.
Handles group tracking and welcome vs start-message logic.

Flow:
  NEW user → /start first time → Welcome message (WelcomeConfig)
  EXISTING user → /start again → Start message (StartMessageConfig)
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    ContextTypes, CommandHandler, ChatMemberHandler
)

import config
from database.models import get_session
from database import db
from bot.services.message_sender import send_configured_message

logger = logging.getLogger(__name__)


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    start_param = args[0] if args else ""

    # Resolve referral
    referrer_id = None
    if start_param.startswith("ref_"):
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
        is_new          = created
        start_msg_shown = db_user.start_msg_shown
        user_db_id      = db_user.id

    if is_new:
        # ── NEW USER: send welcome message ────────────────────────────────
        # Notify admins (if enabled)
        with get_session() as session:
            notify_enabled = db.get_setting(session, "notify_new_user", "true") == "true"
        if notify_enabled:
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

        # Send welcome message
        await _send_welcome(context.bot, user)

    else:
        # ── RETURNING USER: send start message ────────────────────────────
        await _send_start_message(context.bot, user, user_db_id, start_msg_shown)


async def _send_welcome(bot, user):
    """Send the first-time welcome message based on WelcomeConfig."""
    with get_session() as session:
        cfg = db.get_welcome_config(session)

    if not cfg["enabled"]:
        return

    text = cfg.get("text") or ""
    text = text.replace("{name}", user.first_name or "").replace("{id}", str(user.id))

    await send_configured_message(
        bot,
        user.id,
        text=text,
        media_type=cfg["media_type"],
        media_file_id=cfg["media_file_id"],
        protect_content=cfg["protect_content"],
        buttons=cfg["buttons"],
        destruct_seconds=cfg["destruct_seconds"],
    )


async def _send_start_message(bot, user, user_db_id: int, start_msg_shown: bool):
    """Send the returning-user /start message based on StartMessageConfig."""
    with get_session() as session:
        cfg = db.get_start_msg_config(session)

    if not cfg["enabled"]:
        return

    # Check show_mode
    if cfg["show_mode"] == "once_after_join" and start_msg_shown:
        return  # already shown once

    # ── Delete previous /start messages if exist ─────────────────────────
    with get_session() as session:
        old_msg_ids = db.get_last_start_msg_ids(session, user.id)
    for old_id in old_msg_ids:
        try:
            await bot.delete_message(chat_id=user.id, message_id=old_id)
        except Exception:
            pass  # already deleted or expired
        # Cancel any pending self-destruct job for the old message
        with get_session() as session:
            db.cancel_destruct_for_msg(session, user.id, old_id)

    # ── Prepare text ──────────────────────────────────────────────────────
    text = cfg.get("text") or ""
    text = text.replace("{name}", user.first_name or "").replace("{id}", str(user.id))

    sent_ids = []

    # If paid → send invoice instead
    if cfg["is_paid"] and cfg["stars_price"] > 0:
        # Check if already paid
        with get_session() as session:
            already_paid = db.user_has_purchased(session, user_db_id, -1)  # -1 = start msg

        if already_paid:
            sent_ids = await send_configured_message(
                bot, user.id,
                text=text,
                media_type=cfg["media_type"],
                media_file_id=cfg["media_file_id"],
                media_list=cfg.get("media_list", []),
                protect_content=cfg["protect_content"],
                buttons=cfg["buttons"],
                destruct_seconds=cfg["destruct_seconds"],
            )
        else:
            # Send blurred teaser + pay button
            spoiler = cfg["media_type"] in ("photo", "video")
            pay_btn = [[InlineKeyboardButton(
                f"🔓 فتح المحتوى — {cfg['stars_price']} ⭐",
                callback_data="pay_start_msg"
            )]]
            sent_ids = await send_configured_message(
                bot, user.id,
                text=text,
                media_type=cfg["media_type"],
                media_file_id=cfg["media_file_id"],
                media_list=cfg.get("media_list", []),
                protect_content=cfg["protect_content"],
                has_spoiler=spoiler,
                buttons=cfg["buttons"],
                extra_buttons=pay_btn,
                destruct_seconds=0,  # don't destroy until paid
            )
    else:
        # Free — send directly
        sent_ids = await send_configured_message(
            bot, user.id,
            text=text,
            media_type=cfg["media_type"],
            media_file_id=cfg["media_file_id"],
            media_list=cfg.get("media_list", []),
            protect_content=cfg["protect_content"],
            buttons=cfg["buttons"],
            destruct_seconds=cfg["destruct_seconds"],
        )

    # ── Save ALL new message IDs for next deletion ────────────────────────
    if sent_ids:
        with get_session() as session:
            db.save_last_start_msg_ids(session, user.id, sent_ids)

    # Mark as shown for "once_after_join" mode
    if cfg["show_mode"] == "once_after_join":
        with get_session() as session:
            db.mark_start_msg_shown(session, user.id)


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
            for admin_id in config.get_all_admin_ids():
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"➕ <b>البوت أُضيف لمجموعة!</b>\n📌 {chat.title}\n🆔 <code>{chat.id}</code>",
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
                        text=f"➖ <b>البوت أُزيل من مجموعة!</b>\n📌 {chat.title}\n🆔 <code>{chat.id}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass


# ─── Register ─────────────────────────────────────────────────────────────────

def register_user_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  help_cmd))
    app.add_handler(ChatMemberHandler(track_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
