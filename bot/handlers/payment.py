"""
Payment handler: Telegram Stars (XTR) invoice flow.

Flow:
  1. User taps "pay_{message_id}" button  → send_invoice (XTR)
  2. Telegram sends PreCheckoutQuery      → we approve it
  3. Telegram sends successful_payment    → record + deliver content
"""
import logging
from telegram import Update, LabeledPrice
from telegram.ext import (
    ContextTypes, CallbackQueryHandler,
    PreCheckoutQueryHandler, MessageHandler, filters
)

from database.models import get_session
from database import db
from bot.services.broadcast import send_paid_content_to_user
import config

logger = logging.getLogger(__name__)

CURRENCY = "XTR"   # Telegram Stars — provider_token MUST be empty string ""


# ─── Step 1: User taps "pay_{message_id}" ────────────────────────────────────

async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        message_id = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.message.reply_text("❌ طلب غير صالح.")
        return

    user = update.effective_user

    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            await query.message.reply_text("❌ الرسالة غير موجودة أو انتهت.")
            return

        db_user = db.get_user_by_telegram_id(session, user.id)
        if not db_user:
            await query.message.reply_text("❌ يرجى إعادة تشغيل البوت بالأمر /start")
            return

        # Already purchased → re-deliver immediately
        if db.user_has_purchased(session, db_user.id, message_id):
            user_db_id = db_user.id

        else:
            user_db_id = None

        stars = msg.stars_price
        title = msg.title or "محتوى حصري"

    if user_db_id is not None:
        # Already paid — re-send content directly
        await send_paid_content_to_user(context.bot, user.id, user_db_id, message_id)
        await query.message.reply_text("✅ لقد اشتريت هذا المحتوى مسبقاً — تم إرساله مجدداً!")
        return

    # ── Send Stars invoice ────────────────────────────────────────────────────
    # IMPORTANT: provider_token MUST be "" (empty string) for Telegram Stars
    await context.bot.send_invoice(
        chat_id=user.id,
        title=title,
        description="🔓 اضغط على زر الدفع لفتح المحتوى الحصري.",
        payload=f"paid_msg_{message_id}",
        provider_token="",          # ← REQUIRED empty string for XTR Stars
        currency=CURRENCY,
        prices=[LabeledPrice(label="⭐ نجوم تيليجرام", amount=stars)],
        protect_content=True,
    )


# ─── Step 2: Pre-checkout (must answer within 10s) ───────────────────────────

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    payload = query.invoice_payload

    if payload.startswith("paid_msg_"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="طلب دفع غير معروف.")


# ─── Step 3: Successful payment ──────────────────────────────────────────────

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload

    if not payload.startswith("paid_msg_"):
        return

    try:
        message_id = int(payload.split("_")[2])
    except (IndexError, ValueError):
        return

    stars_paid  = payment.total_amount
    charge_id   = payment.telegram_payment_charge_id
    user        = update.effective_user

    with get_session() as session:
        db_user = db.get_user_by_telegram_id(session, user.id)
        if not db_user:
            return

        if not db.user_has_purchased(session, db_user.id, message_id):
            db.record_purchase(session, db_user.id, message_id, stars_paid, charge_id)

        user_db_id = db_user.id

    await update.message.reply_text(
        f"✅ <b>تم الدفع بنجاح!</b>\n"
        f"⭐ دفعت <b>{stars_paid} نجمة</b>\n\n"
        "🔓 جارٍ إرسال المحتوى...",
        parse_mode="HTML",
    )

    await send_paid_content_to_user(context.bot, user.id, user_db_id, message_id)

    # Notify all admins
    for admin_id in config.get_all_admin_ids():
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"💰 <b>دفعة جديدة!</b>\n"
                    f"👤 {user.full_name} (<code>{user.id}</code>)\n"
                    f"⭐ {stars_paid} نجمة\n"
                    f"📨 رسالة #{message_id}"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─── Register ─────────────────────────────────────────────────────────────────

def register_payment_handlers(app):
    app.add_handler(CallbackQueryHandler(pay_callback, pattern=r"^pay_\d+$"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
