"""
User-facing handlers: /start, /mylink, /help, subscription check.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

import config
from database.models import get_session
from database import db
from bot.services import referral as ref_service

logger = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _is_banned(session, telegram_id: int) -> bool:
    user = db.get_user_by_telegram_id(session, telegram_id)
    return user is not None and user.is_banned


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    start_param = args[0] if args else ""

    # Resolve referral
    referrer_id = None
    if start_param:
        token = ref_service.parse_referral_token(start_param)
        if token:
            referrer_id = ref_service.resolve_referrer(token)

    with get_session() as session:
        if _is_banned(session, user.id):
            await update.message.reply_text("🚫 أنت محظور من استخدام هذا البوت.")
            return

        db_user, created = db.get_or_create_user(
            session,
            telegram_id=user.id,
            username=user.username,
            full_name=user.full_name,
            referred_by=referrer_id,
        )

    if created:
        # Notify admin about new subscriber
        for admin_id in config.ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"📥 <b>مشترك جديد!</b>\n"
                        f"👤 الاسم: {user.full_name}\n"
                        f"🆔 المعرف: <code>{user.id}</code>\n"
                        f"📎 عبر دعوة: {'نعم' if referrer_id else 'لا'}"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

    welcome_text = (
        f"👋 أهلاً <b>{user.first_name}</b>!\n\n"
        "🎉 لقد انضممت بنجاح إلى القناة الخاصة.\n"
        "سيتم إرسال المحتوى الحصري إليك مباشرة هنا.\n\n"
        "📌 يمكنك الحصول على رابط دعوتك الخاص بالأمر /mylink"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 رابط الدعوة الخاص بي", callback_data="get_my_link")],
        [InlineKeyboardButton("❓ مساعدة", callback_data="help")],
    ])

    await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=keyboard)


# ─── /mylink ──────────────────────────────────────────────────────────────────

async def my_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_session() as session:
        if _is_banned(session, user.id):
            await update.message.reply_text("🚫 أنت محظور.")
            return
        referral_count = db.get_referral_count(session, user.id)

    token = ref_service.get_or_create_user_invite(user.id)
    invite_url = ref_service.build_invite_url(token)

    await update.message.reply_text(
        f"🔗 <b>رابط دعوتك الخاص:</b>\n"
        f"<code>{invite_url}</code>\n\n"
        f"👥 عدد من دعوتهم: <b>{referral_count}</b>\n\n"
        "📤 شارك هذا الرابط مع أصدقائك!",
        parse_mode="HTML",
    )


# ─── /help ────────────────────────────────────────────────────────────────────

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>المساعدة</b>\n\n"
        "/start - بدء استخدام البوت\n"
        "/mylink - رابط دعوتك الخاص\n"
        "/help - عرض هذه المساعدة\n\n"
        "💬 ستصلك الرسائل الحصرية تلقائياً.",
        parse_mode="HTML",
    )


# ─── Callback: get_my_link / help ─────────────────────────────────────────────

async def user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "get_my_link":
        user = update.effective_user
        token = ref_service.get_or_create_user_invite(user.id)
        invite_url = ref_service.build_invite_url(token)
        with get_session() as session:
            referral_count = db.get_referral_count(session, user.id)
        await query.message.reply_text(
            f"🔗 <b>رابط دعوتك:</b>\n<code>{invite_url}</code>\n\n"
            f"👥 دعوت: <b>{referral_count}</b> شخص",
            parse_mode="HTML",
        )
    elif data == "help":
        await query.message.reply_text(
            "📖 <b>المساعدة</b>\n\n"
            "/mylink - رابط دعوتك\n"
            "/help - هذه القائمة",
            parse_mode="HTML",
        )


# ─── viewed_{view_id} callback (immediate self-destruct) ─────────────────────

async def viewed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🗑️ سيتم حذف الرسالة...")
    data = query.data  # "viewed_<view_id>"
    view_id = int(data.split("_")[1])

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
    app.add_handler(CommandHandler("mylink", my_link))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(user_callback, pattern="^(get_my_link|help)$"))
    app.add_handler(CallbackQueryHandler(viewed_callback, pattern=r"^viewed_\d+$"))
