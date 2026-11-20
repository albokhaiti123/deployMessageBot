"""
User-facing handlers: /start, /help.
Handles invite-only gate, group tracking, and welcome vs start-message logic.

Phase-1 fixes:
  - INVITE_ONLY gate: actually enforced (was dead config before)
  - Referral: read-only lookup first, increment ONLY after confirmed new user
  - Self-referral prevention
  - Paid /start: sends locked teaser ONLY (no content/media/buttons leak)
  - start_msg_shown=True only after successful content delivery
  - html.escape() on all dynamic user/chat values in HTML templates
"""
import html
import logging
from telegram import Update, InlineKeyboardButton
from telegram.ext import ContextTypes, CommandHandler, ChatMemberHandler

import config
from database.models import get_session
from database import db
from bot.services.message_sender import send_configured_message
from bot.services.referral import (
    parse_referral_token, get_referrer_by_token, record_invite_use
)

logger = logging.getLogger(__name__)


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    start_param = args[0] if args else ""

    # FIX: read-only lookup first (does NOT increment uses yet)
    ref_token   = parse_referral_token(start_param)
    referrer_id = get_referrer_by_token(ref_token) if ref_token else None

    # FIX: prevent self-referral
    if referrer_id == user.id:
        referrer_id = None
        ref_token   = None

    with get_session() as session:
        existing = db.get_user_by_telegram_id(session, user.id)
        if existing and existing.is_banned:
            await update.message.reply_text("🚫 أنت محظور من استخدام هذا البوت.")
            return

        # FIX: INVITE_ONLY gate — actually enforced
        if existing is None and config.INVITE_ONLY and not config.is_admin(user.id):
            if referrer_id is None:
                await update.message.reply_text(
                    "🔒 <b>الدخول بالدعوة فقط</b>\n\n"
                    "عذراً، الانضمام إلى هذا البوت متاح عبر رابط دعوة صالح فقط.",
                    parse_mode="HTML",
                )
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
        # FIX: increment uses ONLY for genuinely new users
        if ref_token and referrer_id:
            record_invite_use(ref_token)

        # Notify admins (if enabled)
        with get_session() as session:
            notify_enabled = db.get_setting(session, "notify_new_user", "true") == "true"
        if notify_enabled:
            # FIX: html.escape on user name to prevent HTML injection
            safe_name = html.escape(user.full_name or "—")
            via = f"رابط دعوة (مُحيل: <code>{referrer_id}</code>)" if referrer_id else "مباشر"
            for admin_id in config.get_all_admin_ids():
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"📥 <b>مشترك جديد!</b>\n"
                            f"👤 {safe_name}\n"
                            f"🆔 <code>{user.id}</code>\n"
                            f"📎 {via}"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        await _send_welcome(context.bot, user)

    else:
        await _send_start_message(context.bot, user, user_db_id, start_msg_shown)


async def _send_welcome(bot, user):
    """Send the first-time welcome message based on WelcomeConfig."""
    with get_session() as session:
        cfg = db.get_welcome_config(session)

    if not cfg["enabled"]:
        return

    # FIX: html.escape on name to prevent HTML injection
    safe_first = html.escape(user.first_name or "")
    text = (cfg.get("text") or "").replace("{name}", safe_first).replace("{id}", str(user.id))

    await send_configured_message(
        bot,
        user.id,
        text=text,
        media_type=cfg["media_type"],
        media_file_id=cfg["media_file_id"],
        media_list=cfg.get("media_list", []),
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

    if cfg["show_mode"] == "once_after_join" and start_msg_shown:
        return

    # Delete previous /start messages
    with get_session() as session:
        old_msg_ids = db.get_last_start_msg_ids(session, user.id)
    for old_id in old_msg_ids:
        try:
            await bot.delete_message(chat_id=user.id, message_id=old_id)
        except Exception:
            pass
        with get_session() as session:
            db.cancel_destruct_for_msg(session, user.id, old_id)

    # FIX: html.escape on name
    safe_first = html.escape(user.first_name or "")
    text = (cfg.get("text") or "").replace("{name}", safe_first).replace("{id}", str(user.id))

    sent_ids: list[int] = []
    content_delivered = False

    if cfg["is_paid"] and cfg["stars_price"] > 0:
        # FIX: use purchase_type="start_msg" (no more -1)
        with get_session() as session:
            already_paid = db.user_has_purchased(
                session, user_db_id, purchase_type="start_msg"
            )

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
            content_delivered = bool(sent_ids)
        else:
            # FIX: send ONLY locked teaser — NEVER leak paid content/media/buttons
            media_n = len(cfg.get("media_list", []))
            media_hint = f"\n📎 يحتوي على {media_n} وسائط مرفقة." if media_n > 0 else ""
            teaser = (
                f"🔒 <b>محتوى حصري مدفوع</b>{media_hint}\n\n"
                f"هذا المحتوى مغلق ويتطلب الدفع لفتحه.\n"
                f"⭐ السعر: <b>{cfg['stars_price']} نجمة</b>\n\n"
                f"اضغط على الزر أدناه للدفع وفتح المحتوى."
            )
            pay_btn = [[InlineKeyboardButton(
                f"🔓 فتح المحتوى — {cfg['stars_price']} ⭐",
                callback_data="pay_start_msg"
            )]]
            sent_ids = await send_configured_message(
                bot, user.id,
                text=teaser,
                media_type="none",
                media_file_id=None,
                media_list=[],
                protect_content=cfg["protect_content"],
                has_spoiler=False,
                buttons=[],
                extra_buttons=pay_btn,
                destruct_seconds=0,
            )
            content_delivered = False  # user hasn't paid yet

    else:
        # Free → deliver full content
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
        content_delivered = bool(sent_ids)

    if sent_ids:
        with get_session() as session:
            db.save_last_start_msg_ids(session, user.id, sent_ids)

    # FIX: mark shown ONLY after actual content delivery succeeds
    if content_delivered and cfg["show_mode"] == "once_after_join":
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
    # FIX: html.escape on chat title
    safe_title = html.escape(chat.title or str(chat.id))

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
                        text=(
                            f"➕ <b>البوت أُضيف لمجموعة!</b>\n"
                            f"📌 {safe_title}\n"
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
                            f"📌 {safe_title}\n"
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
    app.add_handler(ChatMemberHandler(track_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
