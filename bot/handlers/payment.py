"""
Payment handler: Telegram Stars (XTR) invoice flow.
"""
import logging
from telegram import Update, LabeledPrice
from telegram.ext import (
    ContextTypes, CallbackQueryHandler,
    PreCheckoutQueryHandler, MessageHandler, filters
)

from database.models import get_session, MediaType
from database import db
from bot.services.broadcast import send_paid_content_to_user

logger = logging.getLogger(__name__)

CURRENCY = "XTR"   # Telegram Stars


# ─── Step 1: User taps "pay_{message_id}" button ─────────────────────────────

async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    message_id = int(query.data.split("_")[1])
    user = update.effective_user

    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            await query.message.reply_text("❌ الرسالة غير موجودة.")
            return

        db_user = db.get_user_by_telegram_id(session, user.id)
        if not db_user:
            await query.message.reply_text("❌ يرجى إعادة تشغيل البوت بالأمر /start")
            return

        # Already purchased?
        if db.user_has_purchased(session, db_user.id, message_id):
            await send_paid_content_to_user(context.bot, user.id, db_user.id, message_id)
            await query.message.reply_text("✅ لقد اشتريت هذا المحتوى مسبقاً. تم إرساله مجدداً!")
            return

        stars = msg.stars_price
        title = msg.title or "محتوى حصري"
        description = "🔓 افتح هذا المحتوى الحصري بعد الدفع."

    # ── Send invoice ──────────────────────────────────────────────────────────
    await context.bot.send_invoice(
        chat_id=user.id,
        title=title,
        description=description,
        payload=f"paid_msg_{message_id}",   # we'll parse this in pre_checkout + success
        currency=CURRENCY,
        prices=[LabeledPrice(label="⭐ نجوم", amount=stars)],
        # provider_token is intentionally empty for Stars
    )


# ─── Step 2: Pre-checkout query (must answer within 10s) ─────────────────────

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    payload = query.invoice_payload

    if payload.startswith("paid_msg_"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="طلب دفع غير صالح.")


# ─── Step 3: Successful payment ───────────────────────────────────────────────

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    user = update.effective_user

    if not payload.startswith("paid_msg_"):
        return

    message_id = int(payload.split("_")[2])
    stars_paid = payment.total_amount
    charge_id = payment.telegram_payment_charge_id

    with get_session() as session:
        db_user = db.get_user_by_telegram_id(session, user.id)
        if not db_user:
            return

        # Prevent duplicate recording
        if not db.user_has_purchased(session, db_user.id, message_id):
            db.record_purchase(session, db_user.id, message_id, stars_paid, charge_id)

        user_db_id = db_user.id

    await update.message.reply_text(
        f"✅ <b>تم الدفع بنجاح!</b>\n"
        f"⭐ دفعت: <b>{stars_paid} نجمة</b>\n\n"
        "🔓 جارٍ إرسال المحتوى...",
        parse_mode="HTML",
    )

    # Send actual content
    await send_paid_content_to_user(context.bot, user.id, user_db_id, message_id)

    # Notify admins
    import config
    for admin_id in config.ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"💰 <b>دفعة جديدة!</b>\n"
                    f"👤 المستخدم: {user.full_name} (<code>{user.id}</code>)\n"
                    f"⭐ النجوم: <b>{stars_paid}</b>\n"
                    f"📨 الرسالة ID: <b>{message_id}</b>"
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
