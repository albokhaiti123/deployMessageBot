"""
Payment handler: Telegram Stars (XTR) invoice flow.

Supports:
  1. Paid broadcast messages (pay_{message_id})
  2. Paid /start message (pay_start_msg)
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
from bot.services.message_sender import send_configured_message
import config

logger = logging.getLogger(__name__)

CURRENCY = "XTR"


# ─── Step 1: User taps pay button ────────────────────────────────────────────

async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    data = query.data

    # ── Paid /start message ───────────────────────────────────────────────
    if data == "pay_start_msg":
        with get_session() as session:
            cfg = db.get_start_msg_config(session)
        if not cfg["is_paid"] or cfg["stars_price"] <= 0:
            await query.message.reply_text("❌ هذا المحتوى مجاني.")
            return

        # Check if already paid
        with get_session() as session:
            db_user = db.get_user_by_telegram_id(session, user.id)
            if db_user and db.user_has_purchased(session, db_user.id, -1):
                # Re-deliver
                text = (cfg.get("text") or "").replace("{name}", user.first_name or "").replace("{id}", str(user.id))
                await send_configured_message(
                    context.bot, user.id,
                    text=text,
                    media_type=cfg["media_type"],
                    media_file_id=cfg["media_file_id"],
                    protect_content=cfg["protect_content"],
                    buttons=cfg["buttons"],
                    destruct_seconds=cfg["destruct_seconds"],
                )
                await query.message.reply_text("✅ سبق أن دفعت — تم إرسال المحتوى!")
                return

        await context.bot.send_invoice(
            chat_id=user.id,
            title="محتوى حصري",
            description="🔓 اضغط على زر الدفع لفتح المحتوى.",
            payload="paid_start_msg",
            provider_token="",
            currency=CURRENCY,
            prices=[LabeledPrice(label="⭐ نجوم", amount=cfg["stars_price"])],
            protect_content=True,
        )
        return

    # ── Paid broadcast message ────────────────────────────────────────────
    try:
        message_id = int(data.split("_")[1])
    except (IndexError, ValueError):
        await query.message.reply_text("❌ طلب غير صالح.")
        return

    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            await query.message.reply_text("❌ الرسالة غير موجودة.")
            return
        db_user = db.get_user_by_telegram_id(session, user.id)
        if not db_user:
            await query.message.reply_text("❌ يرجى إعادة تشغيل البوت بـ /start")
            return
        if db.user_has_purchased(session, db_user.id, message_id):
            user_db_id = db_user.id
            already = True
        else:
            already = False
        stars = msg.stars_price
        title = msg.title or "محتوى حصري"

    if already:
        await send_paid_content_to_user(context.bot, user.id, user_db_id, message_id)
        await query.message.reply_text("✅ سبق أن دفعت — تم إرسال المحتوى!")
        return

    await context.bot.send_invoice(
        chat_id=user.id,
        title=title,
        description="🔓 اضغط على زر الدفع لفتح المحتوى.",
        payload=f"paid_msg_{message_id}",
        provider_token="",
        currency=CURRENCY,
        prices=[LabeledPrice(label="⭐ نجوم", amount=stars)],
        protect_content=True,
    )


# ─── Step 2: Pre-checkout ────────────────────────────────────────────────────

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.pre_checkout_query
    payload = query.invoice_payload

    if payload.startswith("paid_msg_") or payload == "paid_start_msg":
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="طلب دفع غير معروف.")


# ─── Step 3: Successful payment ──────────────────────────────────────────────

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    stars   = payment.total_amount
    charge  = payment.telegram_payment_charge_id
    user    = update.effective_user

    # ── Start message payment ─────────────────────────────────────────────
    if payload == "paid_start_msg":
        with get_session() as session:
            db_user = db.get_user_by_telegram_id(session, user.id)
            if not db_user:
                return
            if not db.user_has_purchased(session, db_user.id, -1):
                db.record_purchase(session, db_user.id, -1, stars, charge)

        with get_session() as session:
            cfg = db.get_start_msg_config(session)

        await update.message.reply_text(
            f"✅ <b>تم الدفع!</b> ⭐ {stars} نجمة\n🔓 جارٍ إرسال المحتوى...",
            parse_mode="HTML",
        )

        text = (cfg.get("text") or "").replace("{name}", user.first_name or "").replace("{id}", str(user.id))
        await send_configured_message(
            context.bot, user.id,
            text=text,
            media_type=cfg["media_type"],
            media_file_id=cfg["media_file_id"],
            protect_content=cfg["protect_content"],
            buttons=cfg["buttons"],
            destruct_seconds=cfg["destruct_seconds"],
        )

        # Mark shown
        with get_session() as session:
            db.mark_start_msg_shown(session, user.id)

        # Notify admins
        for admin_id in config.get_all_admin_ids():
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"💰 <b>دفعة /start!</b>\n👤 {user.full_name} (<code>{user.id}</code>)\n⭐ {stars}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    # ── Broadcast message payment ─────────────────────────────────────────
    if not payload.startswith("paid_msg_"):
        return
    try:
        message_id = int(payload.split("_")[2])
    except (IndexError, ValueError):
        return

    with get_session() as session:
        db_user = db.get_user_by_telegram_id(session, user.id)
        if not db_user:
            return
        if not db.user_has_purchased(session, db_user.id, message_id):
            db.record_purchase(session, db_user.id, message_id, stars, charge)
        user_db_id = db_user.id

    await update.message.reply_text(
        f"✅ <b>تم الدفع!</b> ⭐ {stars} نجمة\n🔓 جارٍ إرسال المحتوى...",
        parse_mode="HTML",
    )
    await send_paid_content_to_user(context.bot, user.id, user_db_id, message_id)

    for admin_id in config.get_all_admin_ids():
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"💰 <b>دفعة جديدة!</b>\n"
                    f"👤 {user.full_name} (<code>{user.id}</code>)\n"
                    f"⭐ {stars} نجمة\n📨 رسالة #{message_id}"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─── Register ─────────────────────────────────────────────────────────────────

def register_payment_handlers(app):
    app.add_handler(CallbackQueryHandler(pay_callback, pattern=r"^pay_"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
