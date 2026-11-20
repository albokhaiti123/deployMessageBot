"""
Payment handler: Telegram Stars (XTR) invoice flow.

Phase-1 fixes:
  - pay_callback: checks is_banned before allowing purchase
  - pay_callback: uses purchase_type="start_msg" (no more -1)
  - pay_callback: re-delivery of start_msg passes media_list
  - pre_checkout: strict validation (banned, already purchased, price match, message exists)
  - successful_payment: uses purchase_type, passes media_list, checks delivery success
  - html.escape() on all user names in HTML notifications
"""
import html
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

    # FIX: verify user exists and is not banned
    with get_session() as session:
        db_user = db.get_user_by_telegram_id(session, user.id)
        if not db_user:
            await query.message.reply_text("❌ يرجى تشغيل البوت بـ /start أولاً.")
            return
        if db_user.is_banned:
            await query.message.reply_text("🚫 أنت محظور من استخدام هذا البوت.")
            return
        user_db_id = db_user.id

    # ── Paid /start message ───────────────────────────────────────────────
    if data == "pay_start_msg":
        with get_session() as session:
            cfg = db.get_start_msg_config(session)
            # FIX: use purchase_type="start_msg"
            already_paid = db.user_has_purchased(
                session, user_db_id, purchase_type="start_msg"
            )

        if not cfg["enabled"] or not cfg["is_paid"] or cfg["stars_price"] <= 0:
            await query.message.reply_text("❌ هذا المحتوى غير متاح للشراء حالياً.")
            return

        if already_paid:
            # FIX: pass media_list on re-delivery
            safe_first = html.escape(user.first_name or "")
            text = (cfg.get("text") or "").replace("{name}", safe_first).replace("{id}", str(user.id))
            sent_ids = await send_configured_message(
                context.bot, user.id,
                text=text,
                media_type=cfg["media_type"],
                media_file_id=cfg["media_file_id"],
                media_list=cfg.get("media_list", []),
                protect_content=cfg["protect_content"],
                buttons=cfg["buttons"],
                destruct_seconds=cfg["destruct_seconds"],
            )
            if sent_ids:
                with get_session() as session:
                    db.save_last_start_msg_ids(session, user.id, sent_ids)
                await query.message.reply_text("✅ سبق أن دفعت — تم إرسال المحتوى!")
            else:
                await query.message.reply_text("⚠️ تعذّر إرسال المحتوى، يرجى المحاولة لاحقاً.")
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
        if not msg.is_paid or (msg.stars_price or 0) <= 0:
            await query.message.reply_text("❌ هذا المحتوى مجاني.")
            return
        # FIX: use purchase_type="broadcast"
        already = db.user_has_purchased(
            session, user_db_id,
            message_id=message_id,
            purchase_type="broadcast",
        )
        stars = msg.stars_price
        title = msg.title or "محتوى حصري"

    if already:
        delivered = await send_paid_content_to_user(
            context.bot, user.id, user_db_id, message_id
        )
        if delivered:
            await query.message.reply_text("✅ سبق أن دفعت — تم إرسال المحتوى!")
        else:
            await query.message.reply_text("⚠️ تعذّر إرسال المحتوى، يرجى المحاولة لاحقاً.")
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


# ─── Step 2: Pre-checkout (strict validation) ────────────────────────────────

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.pre_checkout_query
    payload = query.invoice_payload
    user    = query.from_user

    # FIX: validate currency
    if query.currency != CURRENCY:
        await query.answer(ok=False, error_message="عملة الدفع غير مدعومة.")
        return

    with get_session() as session:
        # FIX: check user exists and not banned
        db_user = db.get_user_by_telegram_id(session, user.id)
        if not db_user:
            await query.answer(ok=False, error_message="يرجى تشغيل البوت بـ /start أولاً.")
            return
        if db_user.is_banned:
            await query.answer(ok=False, error_message="أنت محظور من استخدام هذا البوت.")
            return

        if payload == "paid_start_msg":
            cfg = db.get_start_msg_config(session)
            if not cfg["enabled"] or not cfg["is_paid"] or cfg["stars_price"] <= 0:
                await query.answer(ok=False, error_message="هذا المحتوى لم يعد متاحاً للشراء.")
                return
            # FIX: price match check
            if query.total_amount != cfg["stars_price"]:
                await query.answer(ok=False, error_message="تغيّر السعر، يرجى طلب فاتورة جديدة.")
                return
            # FIX: already purchased check
            if db.user_has_purchased(session, db_user.id, purchase_type="start_msg"):
                await query.answer(ok=False, error_message="لقد قمت بشراء هذا المحتوى مسبقاً.")
                return
            await query.answer(ok=True)
            return

        if payload.startswith("paid_msg_"):
            try:
                message_id = int(payload.split("_")[2])
            except (IndexError, ValueError):
                await query.answer(ok=False, error_message="طلب دفع غير صالح.")
                return

            msg = db.get_message_by_id(session, message_id)
            # FIX: validate message still exists, is paid, price matches
            if not msg or not msg.is_paid or (msg.stars_price or 0) <= 0:
                await query.answer(ok=False, error_message="هذه الرسالة لم تعد متاحة للشراء.")
                return
            if query.total_amount != msg.stars_price:
                await query.answer(ok=False, error_message="تغيّر السعر، يرجى طلب فاتورة جديدة.")
                return
            if db.user_has_purchased(
                session, db_user.id,
                message_id=message_id,
                purchase_type="broadcast",
            ):
                await query.answer(ok=False, error_message="لقد قمت بشراء هذا المحتوى مسبقاً.")
                return
            await query.answer(ok=True)
            return

    await query.answer(ok=False, error_message="طلب دفع غير معروف.")


# ─── Step 3: Successful payment ──────────────────────────────────────────────

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    stars   = payment.total_amount
    charge  = payment.telegram_payment_charge_id
    user    = update.effective_user
    # FIX: html.escape on user name
    safe_full = html.escape(user.full_name or "—")

    # ── Start message payment ─────────────────────────────────────────────
    if payload == "paid_start_msg":
        with get_session() as session:
            db_user = db.get_user_by_telegram_id(session, user.id)
            if not db_user:
                logger.error(f"paid_start_msg from unknown user {user.id}, charge={charge}")
                return
            # FIX: use purchase_type="start_msg"
            if not db.user_has_purchased(session, db_user.id, purchase_type="start_msg"):
                db.record_purchase(
                    session, db_user.id, None, stars, charge, purchase_type="start_msg"
                )
            cfg = db.get_start_msg_config(session)

        await update.message.reply_text(
            f"✅ <b>تم الدفع!</b> ⭐ {stars} نجمة\n🔓 جارٍ إرسال المحتوى...",
            parse_mode="HTML",
        )

        # FIX: pass media_list to deliver full content
        safe_first = html.escape(user.first_name or "")
        text = (cfg.get("text") or "").replace("{name}", safe_first).replace("{id}", str(user.id))
        sent_ids = await send_configured_message(
            context.bot, user.id,
            text=text,
            media_type=cfg["media_type"],
            media_file_id=cfg["media_file_id"],
            media_list=cfg.get("media_list", []),
            protect_content=cfg["protect_content"],
            buttons=cfg["buttons"],
            destruct_seconds=cfg["destruct_seconds"],
        )

        delivery_note = ""
        if sent_ids:
            with get_session() as session:
                db.save_last_start_msg_ids(session, user.id, sent_ids)
                db.mark_start_msg_shown(session, user.id)
        else:
            # FIX: notify user AND admins when delivery fails after payment
            await update.message.reply_text(
                "⚠️ تم تسجيل دفعتك بنجاح ولكن تعذّر إرسال المحتوى.\n"
                "اضغط /start مجدداً أو تواصل مع الإدارة."
            )
            delivery_note = "\n⚠️ <b>فشل تسليم المحتوى — يلزم المراجعة!</b>"

        for admin_id in config.get_all_admin_ids():
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"💰 <b>دفعة /start!</b>\n"
                        f"👤 {safe_full} (<code>{user.id}</code>)\n"
                        f"⭐ {stars}{delivery_note}"
                    ),
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
            logger.error(f"paid_msg #{message_id} from unknown user {user.id}, charge={charge}")
            return
        # FIX: use purchase_type="broadcast"
        if not db.user_has_purchased(
            session, db_user.id,
            message_id=message_id,
            purchase_type="broadcast",
        ):
            db.record_purchase(
                session, db_user.id, message_id, stars, charge, purchase_type="broadcast"
            )
        user_db_id = db_user.id

    await update.message.reply_text(
        f"✅ <b>تم الدفع!</b> ⭐ {stars} نجمة\n🔓 جارٍ إرسال المحتوى...",
        parse_mode="HTML",
    )

    # FIX: check actual delivery success (bool(result) not result is not None)
    delivered = await send_paid_content_to_user(context.bot, user.id, user_db_id, message_id)

    delivery_note = ""
    if not delivered:
        await update.message.reply_text(
            "⚠️ تم تسجيل دفعتك بنجاح ولكن تعذّر إرسال المحتوى.\n"
            "اضغط على زر فتح المحتوى مجدداً أو تواصل مع الإدارة."
        )
        delivery_note = "\n⚠️ <b>فشل تسليم المحتوى — يلزم المراجعة!</b>"

    for admin_id in config.get_all_admin_ids():
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"💰 <b>دفعة جديدة!</b>\n"
                    f"👤 {safe_full} (<code>{user.id}</code>)\n"
                    f"⭐ {stars} نجمة\n📨 رسالة #{message_id}{delivery_note}"
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
