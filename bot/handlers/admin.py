"""
Admin control panel — full conversation-based wizard for creating broadcast messages.
All interactions happen via InlineKeyboard inside the bot.
"""
import logging
import datetime
from functools import wraps
from typing import Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters
)

import config
from database.models import (
    get_session, MediaType, DestructMode, MessageStatus
)
from database import db
from bot.services import scheduler as sched
from bot.services.broadcast import broadcast

logger = logging.getLogger(__name__)

# ─── Conversation states ──────────────────────────────────────────────────────
(
    ADMIN_MENU,
    MSG_TITLE,
    MSG_MEDIA_TYPE,
    MSG_CONTENT,
    MSG_MEDIA_UPLOAD,
    MSG_PAID_CHOICE,
    MSG_STARS_AMOUNT,
    MSG_PROTECT,
    MSG_DESTRUCT_MODE,
    MSG_DESTRUCT_TIME,
    MSG_SCHEDULE_CHOICE,
    MSG_SCHEDULE_TIME,
    MSG_INTERVAL,
    MSG_CONFIRM,
    BROADCAST_MANAGE,
    STATS_VIEW,
    BAN_INPUT,
) = range(17)

# ─── Admin guard decorator ────────────────────────────────────────────────────

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in config.ADMIN_IDS:
            if update.message:
                await update.message.reply_text("🚫 هذا الأمر للمشرفين فقط.")
            elif update.callback_query:
                await update.callback_query.answer("🚫 غير مصرح لك.", show_alert=True)
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapper


# ─── Helper: build main admin menu ───────────────────────────────────────────

def _admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 إنشاء رسالة جديدة",    callback_data="admin_new_msg")],
        [InlineKeyboardButton("📋 إدارة الرسائل",         callback_data="admin_manage")],
        [InlineKeyboardButton("📊 الإحصائيات",            callback_data="admin_stats")],
        [InlineKeyboardButton("🔗 رابط الدعوة الرئيسي",  callback_data="admin_invite")],
        [InlineKeyboardButton("🚫 حظر مستخدم",            callback_data="admin_ban"),
         InlineKeyboardButton("✅ رفع الحظر",             callback_data="admin_unban")],
    ])


# ─── /admin ───────────────────────────────────────────────────────────────────

@admin_only
async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🛠️ <b>لوحة التحكم</b>\n\nاختر الإجراء المطلوب:",
        parse_mode="HTML",
        reply_markup=_admin_menu_keyboard(),
    )
    return ADMIN_MENU


# ─── ADMIN_MENU callbacks ─────────────────────────────────────────────────────

@admin_only
async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "admin_new_msg":
        context.user_data["new_msg"] = {}
        await query.message.reply_text(
            "📝 <b>خطوة 1/9 — العنوان الداخلي</b>\n\n"
            "أدخل عنواناً وصفياً للرسالة (للاستخدام الداخلي فقط):",
            parse_mode="HTML",
        )
        return MSG_TITLE

    elif data == "admin_manage":
        return await _show_messages_list(query, context)

    elif data == "admin_stats":
        return await _show_stats(query, context)

    elif data == "admin_invite":
        from bot.services.referral import get_or_create_user_invite, build_invite_url
        token = get_or_create_user_invite(update.effective_user.id)
        url = build_invite_url(token)
        await query.message.reply_text(
            f"🔗 <b>رابط الدعوة الرئيسي:</b>\n<code>{url}</code>\n\n"
            "شارك هذا الرابط لدعوة المشتركين.",
            parse_mode="HTML",
        )
        return ADMIN_MENU

    elif data == "admin_ban":
        await query.message.reply_text(
            "🚫 أدخل معرف المستخدم (Telegram ID) لحظره:"
        )
        context.user_data["ban_action"] = "ban"
        return BAN_INPUT

    elif data == "admin_unban":
        await query.message.reply_text(
            "✅ أدخل معرف المستخدم لرفع الحظر عنه:"
        )
        context.user_data["ban_action"] = "unban"
        return BAN_INPUT

    elif data == "admin_back":
        await query.message.edit_text(
            "🛠️ <b>لوحة التحكم</b>\n\nاختر الإجراء:",
            parse_mode="HTML",
            reply_markup=_admin_menu_keyboard(),
        )
        return ADMIN_MENU


# ─── Ban/Unban ────────────────────────────────────────────────────────────────

@admin_only
async def ban_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    action = context.user_data.get("ban_action", "ban")
    try:
        target_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ معرف غير صالح. أدخل رقماً فقط.")
        return BAN_INPUT

    with get_session() as session:
        if action == "ban":
            success = db.ban_user(session, target_id)
            msg = f"✅ تم حظر المستخدم <code>{target_id}</code>" if success else "❌ المستخدم غير موجود."
        else:
            success = db.unban_user(session, target_id)
            msg = f"✅ تم رفع الحظر عن <code>{target_id}</code>" if success else "❌ المستخدم غير موجود."

    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=_admin_menu_keyboard())
    return ADMIN_MENU


# ─── Stats ────────────────────────────────────────────────────────────────────

async def _show_stats(query, context):
    with get_session() as session:
        user_count = db.get_user_count(session)
        total_stars = db.get_total_stars_earned(session)
        msg_count = len(db.get_all_messages(session, status=MessageStatus.SENT))

    await query.message.reply_text(
        f"📊 <b>الإحصائيات</b>\n\n"
        f"👥 إجمالي المشتركين: <b>{user_count}</b>\n"
        f"⭐ إجمالي النجوم المكتسبة: <b>{total_stars}</b>\n"
        f"📨 الرسائل المُرسلة: <b>{msg_count}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")
        ]]),
    )
    return ADMIN_MENU


# ─── Manage messages list ─────────────────────────────────────────────────────

async def _show_messages_list(query, context):
    with get_session() as session:
        messages = db.get_all_messages(session)

    if not messages:
        await query.message.reply_text(
            "📭 لا توجد رسائل بعد.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")
            ]]),
        )
        return ADMIN_MENU

    rows = []
    for m in messages[:10]:   # show last 10
        status_icon = {"draft": "📝", "scheduled": "⏰", "sent": "✅", "cancelled": "❌"}.get(m.status.value, "❓")
        paid_icon = "💰" if m.is_paid else "🆓"
        label = f"{status_icon}{paid_icon} [{m.id}] {m.title or 'بدون عنوان'}"
        rows.append([InlineKeyboardButton(label, callback_data=f"mgr_{m.id}")])

    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")])
    await query.message.reply_text(
        "📋 <b>الرسائل (آخر 10):</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return BROADCAST_MANAGE


@admin_only
async def manage_msg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # "mgr_<id>"
    msg_id = int(data.split("_")[1])

    with get_session() as session:
        msg = db.get_message_by_id(session, msg_id)
        if not msg:
            await query.message.reply_text("❌ الرسالة غير موجودة.")
            return ADMIN_MENU

        paid_txt = f"💰 مدفوع ({msg.stars_price} ⭐)" if msg.is_paid else "🆓 مجاني"
        destruct_txt = {
            "never": "🔴 لا تُحذف",
            "immediate": "⚡ فوري",
            "after_time": f"⏱️ بعد {msg.destruct_seconds}s",
        }.get(msg.destruct_mode.value, "—")

        detail = (
            f"📨 <b>تفاصيل الرسالة #{msg.id}</b>\n\n"
            f"📌 العنوان: {msg.title or '—'}\n"
            f"🎬 النوع: {msg.media_type.value}\n"
            f"💵 السعر: {paid_txt}\n"
            f"🔒 حماية المحتوى: {'✅' if msg.protect_content else '❌'}\n"
            f"💣 التدمير: {destruct_txt}\n"
            f"📅 الحالة: {msg.status.value}\n"
            f"🕐 أُنشئت: {msg.created_at.strftime('%Y-%m-%d %H:%M')}"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 إرسال الآن", callback_data=f"send_now_{msg_id}"),
         InlineKeyboardButton("🗑️ حذف", callback_data=f"del_msg_{msg_id}")],
        [InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="admin_manage")],
    ])

    await query.message.edit_text(detail, parse_mode="HTML", reply_markup=keyboard)
    return BROADCAST_MANAGE


@admin_only
async def send_now_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ جارٍ الإرسال...")
    msg_id = int(query.data.split("_")[2])
    await query.message.edit_text("⏳ <b>جارٍ البث...</b>", parse_mode="HTML")
    stats = await broadcast(context.bot, msg_id)
    await query.message.edit_text(
        f"✅ <b>اكتمل البث!</b>\n\n"
        f"📤 الإجمالي: {stats.get('total', 0)}\n"
        f"✅ نجح: {stats.get('success', 0)}\n"
        f"❌ فشل: {stats.get('failed', 0)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage")
        ]]),
    )
    return BROADCAST_MANAGE


@admin_only
async def del_msg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    msg_id = int(query.data.split("_")[2])
    sched.cancel_broadcast(msg_id)
    with get_session() as session:
        db.delete_message_record(session, msg_id)
    await query.message.edit_text(
        f"🗑️ تم حذف الرسالة #{msg_id}.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage")
        ]]),
    )
    return BROADCAST_MANAGE


# ═══════════════════════════════════════════════════════════════════════════════
# NEW MESSAGE WIZARD
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Step 1: Title ────────────────────────────────────────────────────────────

@admin_only
async def msg_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    context.user_data["new_msg"]["title"] = title

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 نص فقط",   callback_data="mtype_none"),
         InlineKeyboardButton("🖼️ صورة",     callback_data="mtype_photo")],
        [InlineKeyboardButton("🎥 فيديو",    callback_data="mtype_video"),
         InlineKeyboardButton("🎵 صوت",      callback_data="mtype_audio")],
        [InlineKeyboardButton("📎 ملف",      callback_data="mtype_document")],
    ])
    await update.message.reply_text(
        "📂 <b>خطوة 2/9 — نوع الوسائط</b>\n\nاختر نوع محتوى الرسالة:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return MSG_MEDIA_TYPE


# ─── Step 2: Media type ───────────────────────────────────────────────────────

@admin_only
async def msg_media_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mtype = query.data.split("_")[1]   # none/photo/video/audio/document
    context.user_data["new_msg"]["media_type"] = mtype

    if mtype == "none":
        await query.message.reply_text(
            "✍️ <b>خطوة 3/9 — النص</b>\n\nأدخل نص الرسالة (يدعم HTML):",
            parse_mode="HTML",
        )
        return MSG_CONTENT
    else:
        await query.message.reply_text(
            f"📤 <b>خطوة 3/9 — رفع الوسائط</b>\n\n"
            f"أرسل ملف الـ {mtype} الآن.\n"
            f"<i>يمكنك أيضاً إضافة نص (caption) مع الملف مباشرة.</i>",
            parse_mode="HTML",
        )
        return MSG_MEDIA_UPLOAD


# ─── Step 3a: Text content (no media) ────────────────────────────────────────

@admin_only
async def msg_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_msg"]["text"] = update.message.text
    context.user_data["new_msg"]["media_file_id"] = None
    return await _ask_paid(update, context)


# ─── Step 3b: Media upload ────────────────────────────────────────────────────

@admin_only
async def msg_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mtype = context.user_data["new_msg"]["media_type"]
    file_id = None
    caption = None

    if mtype == "photo" and update.message.photo:
        file_id = update.message.photo[-1].file_id
        caption = update.message.caption
    elif mtype == "video" and update.message.video:
        file_id = update.message.video.file_id
        caption = update.message.caption
    elif mtype == "audio" and update.message.audio:
        file_id = update.message.audio.file_id
        caption = update.message.caption
    elif mtype == "document" and update.message.document:
        file_id = update.message.document.file_id
        caption = update.message.caption

    if not file_id:
        await update.message.reply_text("❌ لم يتم استلام الملف. أعد الإرسال.")
        return MSG_MEDIA_UPLOAD

    context.user_data["new_msg"]["media_file_id"] = file_id
    context.user_data["new_msg"]["text"] = caption
    return await _ask_paid(update, context)


async def _ask_paid(update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🆓 مجاني (مفتوح للجميع)", callback_data="paid_no")],
        [InlineKeyboardButton("💰 مدفوع (يتطلب نجوم ⭐)", callback_data="paid_yes")],
    ])
    await update.message.reply_text(
        "💵 <b>خطوة 4/9 — نوع الوصول</b>\n\nهل الرسالة مجانية أم مدفوعة؟",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return MSG_PAID_CHOICE


# ─── Step 4: Paid choice ─────────────────────────────────────────────────────

@admin_only
async def msg_paid_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    is_paid = query.data == "paid_yes"
    context.user_data["new_msg"]["is_paid"] = is_paid

    if is_paid:
        await query.message.reply_text(
            "⭐ <b>خطوة 5/9 — سعر النجوم</b>\n\nأدخل عدد النجوم (مثال: 50):",
            parse_mode="HTML",
        )
        return MSG_STARS_AMOUNT
    else:
        context.user_data["new_msg"]["stars_price"] = 0
        return await _ask_protect(query, context)


# ─── Step 5: Stars amount ─────────────────────────────────────────────────────

@admin_only
async def msg_stars_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        stars = int(update.message.text.strip())
        if stars < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ أدخل رقماً صحيحاً أكبر من صفر.")
        return MSG_STARS_AMOUNT

    context.user_data["new_msg"]["stars_price"] = stars
    return await _ask_protect(update, context)


async def _ask_protect(update_or_query, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 نعم — حماية المحتوى", callback_data="protect_yes")],
        [InlineKeyboardButton("🔓 لا — السماح بالتوجيه", callback_data="protect_no")],
    ])
    text = (
        "🔐 <b>خطوة 6/9 — حماية المحتوى</b>\n\n"
        "هل تريد منع التوجيه (Forward) ونسخ الرسالة؟\n"
        "<i>ملاحظة: لا يمنع لقطة الشاشة كلياً.</i>"
    )
    if hasattr(update_or_query, "message"):
        await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    return MSG_PROTECT


# ─── Step 6: Protect content ─────────────────────────────────────────────────

@admin_only
async def msg_protect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_msg"]["protect_content"] = (query.data == "protect_yes")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 لا تُحذف أبداً",          callback_data="dest_never")],
        [InlineKeyboardButton("⚡ حذف فوري بعد المشاهدة",   callback_data="dest_immediate")],
        [InlineKeyboardButton("⏱️ حذف بعد فترة زمنية",     callback_data="dest_after_time")],
    ])
    await query.message.reply_text(
        "💣 <b>خطوة 7/9 — ذاتية التدمير</b>\n\nمتى يتم حذف الرسالة؟",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return MSG_DESTRUCT_MODE


# ─── Step 7: Destruct mode ───────────────────────────────────────────────────

@admin_only
async def msg_destruct_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode_raw = query.data.split("_", 1)[1]   # never / immediate / after_time
    context.user_data["new_msg"]["destruct_mode"] = mode_raw

    if mode_raw == "after_time":
        await query.message.reply_text(
            "⏱️ <b>خطوة 7b — مدة التدمير</b>\n\n"
            "أدخل عدد الثواني قبل الحذف (مثال: 60 = دقيقة واحدة):",
            parse_mode="HTML",
        )
        return MSG_DESTRUCT_TIME
    else:
        context.user_data["new_msg"]["destruct_seconds"] = 0
        return await _ask_schedule(query, context)


# ─── Step 7b: Destruct time ───────────────────────────────────────────────────

@admin_only
async def msg_destruct_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        secs = int(update.message.text.strip())
        if secs < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ أدخل رقماً أكبر من صفر.")
        return MSG_DESTRUCT_TIME
    context.user_data["new_msg"]["destruct_seconds"] = secs
    return await _ask_schedule(update, context)


async def _ask_schedule(update_or_query, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 إرسال فوري", callback_data="sched_now")],
        [InlineKeyboardButton("📅 جدولة وقت محدد", callback_data="sched_time")],
        [InlineKeyboardButton("🔁 إرسال متكرر", callback_data="sched_repeat")],
    ])
    text = "⏰ <b>خطوة 8/9 — جدولة الإرسال</b>\n\nمتى ترسل الرسالة؟"
    if hasattr(update_or_query, "message"):
        await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    return MSG_SCHEDULE_CHOICE


# ─── Step 8: Schedule choice ─────────────────────────────────────────────────

@admin_only
async def msg_schedule_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data  # sched_now / sched_time / sched_repeat

    context.user_data["new_msg"]["schedule_choice"] = choice

    if choice == "sched_now":
        context.user_data["new_msg"]["scheduled_at"] = None
        context.user_data["new_msg"]["interval_hours"] = None
        return await _show_confirm(query, context)

    elif choice == "sched_time":
        await query.message.reply_text(
            "📅 <b>خطوة 8b — وقت الإرسال</b>\n\n"
            "أدخل التاريخ والوقت بالصيغة التالية:\n"
            "<code>YYYY-MM-DD HH:MM</code>\n"
            "مثال: <code>2025-12-31 18:00</code>",
            parse_mode="HTML",
        )
        return MSG_SCHEDULE_TIME

    elif choice == "sched_repeat":
        await query.message.reply_text(
            "🔁 <b>خطوة 8b — التكرار</b>\n\n"
            "أدخل عدد الساعات بين كل إرسال (مثال: 24 = يومياً):",
            parse_mode="HTML",
        )
        return MSG_INTERVAL


# ─── Step 8b: Schedule time ───────────────────────────────────────────────────

@admin_only
async def msg_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text("❌ صيغة غير صحيحة. المثال: 2025-12-31 18:00")
        return MSG_SCHEDULE_TIME
    context.user_data["new_msg"]["scheduled_at"] = dt
    context.user_data["new_msg"]["interval_hours"] = None
    return await _show_confirm(update, context)


# ─── Step 8c: Interval ────────────────────────────────────────────────────────

@admin_only
async def msg_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hours = float(update.message.text.strip())
        if hours <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ أدخل رقماً أكبر من صفر.")
        return MSG_INTERVAL
    context.user_data["new_msg"]["interval_hours"] = hours
    context.user_data["new_msg"]["scheduled_at"] = None
    return await _show_confirm(update, context)


# ─── Step 9: Confirm ─────────────────────────────────────────────────────────

async def _show_confirm(update_or_query, context):
    d = context.user_data["new_msg"]
    paid_txt = f"💰 {d.get('stars_price', 0)} ⭐" if d.get("is_paid") else "🆓 مجاني"
    destruct_map = {
        "never": "لا تُحذف",
        "immediate": "فوري بعد المشاهدة",
        "after_time": f"بعد {d.get('destruct_seconds', 0)} ثانية",
    }
    sched_txt = (
        f"متكرر كل {d.get('interval_hours')} ساعة" if d.get("interval_hours")
        else (str(d.get("scheduled_at")) if d.get("scheduled_at") else "الآن")
    )

    summary = (
        f"📋 <b>ملخص الرسالة — خطوة 9/9</b>\n\n"
        f"📌 العنوان: {d.get('title', '—')}\n"
        f"🎬 النوع: {d.get('media_type', 'none')}\n"
        f"💵 الوصول: {paid_txt}\n"
        f"🔒 حماية: {'✅' if d.get('protect_content') else '❌'}\n"
        f"💣 التدمير: {destruct_map.get(d.get('destruct_mode', 'never'), '—')}\n"
        f"⏰ الإرسال: {sched_txt}\n\n"
        "هل تريد تأكيد الإنشاء؟"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد وإنشاء", callback_data="confirm_create"),
         InlineKeyboardButton("❌ إلغاء",         callback_data="cancel_create")],
    ])
    if hasattr(update_or_query, "message"):
        await update_or_query.message.reply_text(summary, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update_or_query.message.reply_text(summary, parse_mode="HTML", reply_markup=keyboard)
    return MSG_CONFIRM


# ─── Step 9: Confirm callback ─────────────────────────────────────────────────

@admin_only
async def msg_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel_create":
        await query.message.edit_text("❌ تم إلغاء العملية.")
        context.user_data.clear()
        return ConversationHandler.END

    d = context.user_data["new_msg"]
    media_type_map = {
        "none": MediaType.NONE,
        "photo": MediaType.PHOTO,
        "video": MediaType.VIDEO,
        "audio": MediaType.AUDIO,
        "document": MediaType.DOCUMENT,
    }
    destruct_map = {
        "never": DestructMode.NEVER,
        "immediate": DestructMode.IMMEDIATE,
        "after_time": DestructMode.AFTER_TIME,
    }
    status = (
        MessageStatus.DRAFT
        if d.get("scheduled_at") or d.get("interval_hours")
        else MessageStatus.DRAFT
    )

    with get_session() as session:
        msg = db.create_broadcast_message(
            session,
            title=d.get("title"),
            text=d.get("text"),
            media_type=media_type_map.get(d.get("media_type", "none"), MediaType.NONE),
            media_file_id=d.get("media_file_id"),
            is_paid=d.get("is_paid", False),
            stars_price=d.get("stars_price", 0),
            protect_content=d.get("protect_content", True),
            destruct_mode=destruct_map.get(d.get("destruct_mode", "never"), DestructMode.NEVER),
            destruct_seconds=d.get("destruct_seconds", 0),
            scheduled_at=d.get("scheduled_at"),
            interval_hours=d.get("interval_hours"),
            status=MessageStatus.DRAFT,
            created_by=update.effective_user.id,
        )
        msg_id = msg.id

    schedule_choice = d.get("schedule_choice", "sched_now")
    if schedule_choice == "sched_now":
        await query.message.edit_text("⏳ <b>جارٍ البث الآن...</b>", parse_mode="HTML")
        stats = await broadcast(context.bot, msg_id)
        await query.message.reply_text(
            f"✅ <b>اكتمل البث!</b>\n\n"
            f"📤 الإجمالي: {stats.get('total', 0)}\n"
            f"✅ نجح: {stats.get('success', 0)}\n"
            f"❌ فشل: {stats.get('failed', 0)}",
            parse_mode="HTML",
            reply_markup=_admin_menu_keyboard(),
        )
    else:
        db_status = MessageStatus.SCHEDULED
        with get_session() as session:
            db.update_message_status(session, msg_id, db_status)
        sched.schedule_broadcast(
            context.bot, msg_id,
            run_at=d.get("scheduled_at"),
            interval_hours=d.get("interval_hours"),
        )
        await query.message.edit_text(
            f"✅ <b>تم جدولة الرسالة #{msg_id}</b>",
            parse_mode="HTML",
            reply_markup=_admin_menu_keyboard(),
        )

    context.user_data.clear()
    return ConversationHandler.END


# ─── Cancel command ───────────────────────────────────────────────────────────

@admin_only
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ تم الإلغاء.", reply_markup=_admin_menu_keyboard())
    return ConversationHandler.END


# ─── Register all admin handlers ──────────────────────────────────────────────

def register_admin_handlers(app):
    conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_start)],
        states={
            ADMIN_MENU: [
                CallbackQueryHandler(admin_menu_callback,
                    pattern="^(admin_new_msg|admin_manage|admin_stats|admin_invite|admin_ban|admin_unban|admin_back)$"),
            ],
            MSG_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_title)],
            MSG_MEDIA_TYPE: [CallbackQueryHandler(msg_media_type, pattern=r"^mtype_")],
            MSG_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_content)],
            MSG_MEDIA_UPLOAD: [
                MessageHandler(
                    (filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.Document.ALL)
                    & ~filters.COMMAND,
                    msg_media_upload,
                )
            ],
            MSG_PAID_CHOICE: [CallbackQueryHandler(msg_paid_choice, pattern=r"^paid_")],
            MSG_STARS_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_stars_amount)],
            MSG_PROTECT: [CallbackQueryHandler(msg_protect, pattern=r"^protect_")],
            MSG_DESTRUCT_MODE: [CallbackQueryHandler(msg_destruct_mode, pattern=r"^dest_")],
            MSG_DESTRUCT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_destruct_time)],
            MSG_SCHEDULE_CHOICE: [CallbackQueryHandler(msg_schedule_choice, pattern=r"^sched_")],
            MSG_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_schedule_time)],
            MSG_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_interval)],
            MSG_CONFIRM: [CallbackQueryHandler(msg_confirm, pattern=r"^(confirm_create|cancel_create)$")],
            BROADCAST_MANAGE: [
                CallbackQueryHandler(manage_msg_callback,     pattern=r"^mgr_\d+$"),
                CallbackQueryHandler(send_now_callback,       pattern=r"^send_now_\d+$"),
                CallbackQueryHandler(del_msg_callback,        pattern=r"^del_msg_\d+$"),
                CallbackQueryHandler(admin_menu_callback,     pattern="^admin_(manage|back)$"),
            ],
            BAN_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ban_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )
    app.add_handler(conv)
