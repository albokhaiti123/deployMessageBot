"""
Admin control panel — simplified wizard + cancel buttons everywhere.

Wizard flow (4-6 steps instead of 8):
  Step 1: Send content directly (text / photo+caption / video / audio / doc)
           → type auto-detected, no separate type selection step
  Step 2: Free or Paid?
  Step 3: Stars amount (only if paid)
  Step 4: Protect content?
  Step 5: Self-destruct seconds (0 = never)
  Step 6: Broadcast target (users / groups / all)
  Step 7: Schedule (now / time / repeat)
  Confirm → send or schedule

Cancel button present at EVERY step.
"""
import logging
import datetime
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters
)

import config
from database.models import get_session, MediaType, DestructMode, MessageStatus
from database import db
from bot.services import scheduler as sched
from bot.services.broadcast import broadcast

logger = logging.getLogger(__name__)

# ─── States ──────────────────────────────────────────────────────────────────
(
    ADMIN_MENU,
    MSG_CONTENT,          # single step: send content of any type
    MSG_PAID_CHOICE,
    MSG_STARS_AMOUNT,
    MSG_PROTECT,
    MSG_DESTRUCT_SECONDS,
    MSG_BROADCAST_TARGET,
    MSG_SCHEDULE_CHOICE,
    MSG_SCHEDULE_TIME,
    MSG_INTERVAL,
    MSG_CONFIRM,
    BROADCAST_MANAGE,
    BAN_INPUT,
    SETTINGS_MENU,
    WELCOME_TEXT_INPUT,
    ADD_ADMIN_INPUT,
    REMOVE_ADMIN_INPUT,
    INVITE_MENU,
) = range(18)

# ─── Shared cancel keyboard ───────────────────────────────────────────────────

def _cancel_kb(label: str = "❌ إلغاء") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data="wizard_cancel")
    ]])


def _back_cancel_kb(back_data: str, back_label: str = "🔙 رجوع") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(back_label, callback_data=back_data),
        InlineKeyboardButton("❌ إلغاء", callback_data="wizard_cancel"),
    ]])


# ─── Admin guard ──────────────────────────────────────────────────────────────

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid = update.effective_user.id
        if not config.is_admin(uid):
            if update.message:
                await update.message.reply_text("🚫 هذا الأمر للمشرفين فقط.")
            elif update.callback_query:
                await update.callback_query.answer("🚫 غير مصرح لك.", show_alert=True)
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapper


# ─── Wizard cancel handler ────────────────────────────────────────────────────

@admin_only
async def wizard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.message.edit_text(
        "❌ <b>تم إلغاء العملية.</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 العودة للقائمة", callback_data="go_menu")
        ]]),
    )
    return ADMIN_MENU


@admin_only
async def go_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(
        "🛠️ <b>لوحة التحكم</b>\n\nاختر الإجراء:",
        parse_mode="HTML",
        reply_markup=_admin_menu_kb(),
    )
    return ADMIN_MENU


# ─── Main menu ────────────────────────────────────────────────────────────────

def _admin_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 إنشاء رسالة بث",       callback_data="admin_new_msg")],
        [InlineKeyboardButton("📋 إدارة الرسائل",          callback_data="admin_manage")],
        [InlineKeyboardButton("📊 الإحصائيات",             callback_data="admin_stats")],
        [InlineKeyboardButton("🔗 توليد رابط دعوة",        callback_data="admin_invite")],
        [InlineKeyboardButton("⚙️ إعدادات البوت",          callback_data="admin_settings")],
        [InlineKeyboardButton("👑 إدارة المشرفين",         callback_data="admin_admins")],
        [InlineKeyboardButton("🚫 حظر مستخدم",            callback_data="admin_ban"),
         InlineKeyboardButton("✅ رفع حظر",               callback_data="admin_unban")],
    ])


@admin_only
async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🛠️ <b>لوحة التحكم</b>\n\nاختر الإجراء:",
        parse_mode="HTML",
        reply_markup=_admin_menu_kb(),
    )
    return ADMIN_MENU


# ─── ADMIN_MENU dispatch ──────────────────────────────────────────────────────

@admin_only
async def admin_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "admin_new_msg":
        context.user_data["msg"] = {}
        await query.message.reply_text(
            "📝 <b>أرسل محتوى الرسالة الآن</b>\n\n"
            "يمكنك إرسال:\n"
            "• رسالة نصية (تدعم HTML)\n"
            "• صورة مع نص (caption)\n"
            "• فيديو مع نص\n"
            "• ملف صوتي مع نص\n"
            "• مستند مع نص\n\n"
            "<i>⚠️ أرسل رسالة واحدة فقط تحتوي على كل ما تريد.</i>",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return MSG_CONTENT

    if data == "admin_manage":
        return await _show_messages_list(query, context)

    if data == "admin_stats":
        return await _show_stats(query)

    if data == "admin_invite":
        return await _show_invite_menu(query, context)

    if data == "admin_settings":
        return await _show_settings_menu(query, context)

    if data == "admin_admins":
        return await _show_admins_menu(query, context)

    if data == "admin_ban":
        await query.message.reply_text(
            "🚫 أدخل معرف المستخدم (Telegram ID) لحظره:\n\n"
            "أو أرسل /cancel للإلغاء",
            reply_markup=_cancel_kb(),
        )
        context.user_data["ban_action"] = "ban"
        return BAN_INPUT

    if data == "admin_unban":
        await query.message.reply_text(
            "✅ أدخل معرف المستخدم لرفع الحظر:\n\n"
            "أو أرسل /cancel للإلغاء",
            reply_markup=_cancel_kb(),
        )
        context.user_data["ban_action"] = "unban"
        return BAN_INPUT

    if data in ("admin_back", "go_menu"):
        try:
            await query.message.edit_text(
                "🛠️ <b>لوحة التحكم</b>\n\nاختر الإجراء:",
                parse_mode="HTML",
                reply_markup=_admin_menu_kb(),
            )
        except Exception:
            await query.message.reply_text(
                "🛠️ <b>لوحة التحكم</b>",
                parse_mode="HTML",
                reply_markup=_admin_menu_kb(),
            )
        return ADMIN_MENU

    return ADMIN_MENU


# ═══════════════════════════════════════════════════════════════════════════════
# WIZARD STEP 1 — Content (auto-detect type)
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def msg_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive any message type and auto-detect content."""
    m   = update.message
    msg = context.user_data.setdefault("msg", {})

    if m.photo:
        msg["media_type"]    = "photo"
        msg["media_file_id"] = m.photo[-1].file_id
        msg["text"]          = m.caption
    elif m.video:
        msg["media_type"]    = "video"
        msg["media_file_id"] = m.video.file_id
        msg["text"]          = m.caption
    elif m.audio:
        msg["media_type"]    = "audio"
        msg["media_file_id"] = m.audio.file_id
        msg["text"]          = m.caption
    elif m.voice:
        msg["media_type"]    = "audio"
        msg["media_file_id"] = m.voice.file_id
        msg["text"]          = m.caption
    elif m.document:
        msg["media_type"]    = "document"
        msg["media_file_id"] = m.document.file_id
        msg["text"]          = m.caption
    elif m.text:
        msg["media_type"]    = "none"
        msg["media_file_id"] = None
        msg["text"]          = m.text
    else:
        await m.reply_text(
            "❌ نوع الرسالة غير مدعوم. أرسل نصاً أو صورة أو فيديو أو صوتاً أو مستنداً.",
            reply_markup=_cancel_kb(),
        )
        return MSG_CONTENT

    type_icons = {
        "none": "📝 نص", "photo": "🖼️ صورة", "video": "🎥 فيديو",
        "audio": "🎵 صوت", "document": "📎 مستند"
    }
    detected = type_icons.get(msg["media_type"], "📄")

    await m.reply_text(
        f"✅ تم استلام المحتوى ({detected})\n\n"
        "💵 <b>خطوة 2 — نوع الوصول</b>\n\nهل الرسالة مجانية أم مدفوعة؟",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🆓 مجاني — للجميع",      callback_data="paid_no")],
            [InlineKeyboardButton("💰 مدفوع — بالنجوم ⭐",  callback_data="paid_yes")],
            [InlineKeyboardButton("❌ إلغاء",               callback_data="wizard_cancel")],
        ]),
    )
    return MSG_PAID_CHOICE


# ═══════════════════════════════════════════════════════════════════════════════
# WIZARD STEP 2 — Paid / Free
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def msg_paid_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    is_paid = (query.data == "paid_yes")
    context.user_data["msg"]["is_paid"] = is_paid

    if is_paid:
        await query.message.reply_text(
            "⭐ <b>خطوة 3 — سعر النجوم</b>\n\n"
            "أدخل عدد النجوم المطلوبة (الحد الأدنى 1):",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return MSG_STARS_AMOUNT

    context.user_data["msg"]["stars_price"] = 0
    return await _ask_protect(query)


@admin_only
async def msg_stars_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        stars = int(update.message.text.strip())
        assert stars >= 1
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ أدخل رقماً صحيحاً أكبر من صفر.",
            reply_markup=_cancel_kb(),
        )
        return MSG_STARS_AMOUNT

    context.user_data["msg"]["stars_price"] = stars
    return await _ask_protect(update)


async def _ask_protect(obj):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 نعم — منع التوجيه والنسخ", callback_data="protect_yes")],
        [InlineKeyboardButton("🔓 لا — السماح بالتوجيه",    callback_data="protect_no")],
        [InlineKeyboardButton("❌ إلغاء",                    callback_data="wizard_cancel")],
    ])
    text = (
        "🔐 <b>خطوة — حماية المحتوى</b>\n\n"
        "منع التوجيه (Forward) ونسخ الرسالة؟\n"
        "<i>لا يمنع لقطة الشاشة — قيد تليجرام.</i>"
    )
    m = obj.message if hasattr(obj, "message") else obj
    await m.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return MSG_PROTECT


# ═══════════════════════════════════════════════════════════════════════════════
# WIZARD STEP — Protect
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def msg_protect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["msg"]["protect_content"] = (query.data == "protect_yes")

    await query.message.reply_text(
        "💣 <b>خطوة — ذاتية التدمير</b>\n\n"
        "بعد كم ثانية تُحذف الرسالة تلقائياً من دردشة المستخدم؟\n\n"
        "• <b>0</b> = لا تُحذف أبداً\n"
        "• <b>30</b> = تُحذف بعد 30 ثانية\n"
        "• <b>3600</b> = تُحذف بعد ساعة\n\n"
        "<i>الحذف يبدأ من لحظة تسليم الرسالة.</i>",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )
    return MSG_DESTRUCT_SECONDS


# ═══════════════════════════════════════════════════════════════════════════════
# WIZARD STEP — Destruct seconds
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def msg_destruct_seconds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        secs = int(update.message.text.strip())
        assert secs >= 0
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ أدخل رقماً صحيحاً (0 أو أكبر).",
            reply_markup=_cancel_kb(),
        )
        return MSG_DESTRUCT_SECONDS

    context.user_data["msg"]["destruct_seconds"] = secs
    context.user_data["msg"]["destruct_mode"]    = "after_time" if secs > 0 else "never"
    return await _ask_broadcast_target(update.message)


# ═══════════════════════════════════════════════════════════════════════════════
# WIZARD STEP — Broadcast target
# ═══════════════════════════════════════════════════════════════════════════════

async def _ask_broadcast_target(msg_obj):
    with get_session() as session:
        group_count = db.get_group_count(session)
        user_count  = db.get_user_count(session)

    await msg_obj.reply_text(
        f"📡 <b>خطوة — هدف البث</b>\n\n"
        f"👥 المشتركون: <b>{user_count}</b>\n"
        f"💬 المجموعات النشطة: <b>{group_count}</b>\n\n"
        "اختر من تريد إرسال الرسالة إليهم:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"👥 المشتركون فقط ({user_count})",    callback_data="target_users")],
            [InlineKeyboardButton(f"💬 المجموعات فقط ({group_count})",   callback_data="target_groups")],
            [InlineKeyboardButton(f"🌐 الكل ({user_count + group_count})", callback_data="target_all")],
            [InlineKeyboardButton("❌ إلغاء",                             callback_data="wizard_cancel")],
        ]),
    )
    return MSG_BROADCAST_TARGET


@admin_only
async def msg_broadcast_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    target_map = {
        "target_users":  "users",
        "target_groups": "groups",
        "target_all":    "all",
    }
    context.user_data["msg"]["broadcast_target"] = target_map.get(query.data, "users")
    return await _ask_schedule(query.message)


# ═══════════════════════════════════════════════════════════════════════════════
# WIZARD STEP — Schedule
# ═══════════════════════════════════════════════════════════════════════════════

async def _ask_schedule(msg_obj):
    await msg_obj.reply_text(
        "⏰ <b>خطوة — جدولة الإرسال</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 إرسال فوري",           callback_data="sched_now")],
            [InlineKeyboardButton("📅 وقت محدد",              callback_data="sched_time")],
            [InlineKeyboardButton("🔁 متكرر (كل N ساعة)",    callback_data="sched_repeat")],
            [InlineKeyboardButton("❌ إلغاء",                 callback_data="wizard_cancel")],
        ]),
    )
    return MSG_SCHEDULE_CHOICE


@admin_only
async def msg_schedule_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    choice = query.data
    context.user_data["msg"]["schedule_choice"] = choice

    if choice == "sched_now":
        context.user_data["msg"]["scheduled_at"]   = None
        context.user_data["msg"]["interval_hours"] = None
        return await _show_confirm(query.message, context)

    if choice == "sched_time":
        await query.message.reply_text(
            "📅 أدخل التاريخ والوقت:\n"
            "الصيغة: <code>YYYY-MM-DD HH:MM</code>\n"
            "مثال: <code>2025-12-31 20:00</code>",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return MSG_SCHEDULE_TIME

    if choice == "sched_repeat":
        await query.message.reply_text(
            "🔁 أدخل عدد الساعات بين كل إرسال:\n"
            "مثال: <code>24</code> = يومياً",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return MSG_INTERVAL


@admin_only
async def msg_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        dt = datetime.datetime.strptime(update.message.text.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text(
            "❌ الصيغة خاطئة. مثال: 2025-12-31 20:00",
            reply_markup=_cancel_kb(),
        )
        return MSG_SCHEDULE_TIME

    context.user_data["msg"]["scheduled_at"]   = dt
    context.user_data["msg"]["interval_hours"] = None
    return await _show_confirm(update.message, context)


@admin_only
async def msg_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hours = float(update.message.text.strip())
        assert hours > 0
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ أدخل رقماً أكبر من صفر.",
            reply_markup=_cancel_kb(),
        )
        return MSG_INTERVAL

    context.user_data["msg"]["interval_hours"] = hours
    context.user_data["msg"]["scheduled_at"]   = None
    return await _show_confirm(update.message, context)


# ═══════════════════════════════════════════════════════════════════════════════
# WIZARD — Confirm
# ═══════════════════════════════════════════════════════════════════════════════

async def _show_confirm(msg_obj, context):
    d = context.user_data["msg"]

    type_icons = {
        "none": "📝 نص", "photo": "🖼️ صورة", "video": "🎥 فيديو",
        "audio": "🎵 صوت", "document": "📎 مستند"
    }
    target_labels = {"users": "👥 المشتركون", "groups": "💬 المجموعات", "all": "🌐 الكل"}
    paid_txt  = f"💰 {d.get('stars_price', 0)} ⭐" if d.get("is_paid") else "🆓 مجاني"
    secs      = d.get("destruct_seconds", 0)
    dest_txt  = "🔴 لا تُحذف" if secs == 0 else f"⏱️ بعد {secs} ثانية"
    sched_txt = (
        f"🔁 كل {d['interval_hours']}h" if d.get("interval_hours")
        else str(d.get("scheduled_at")) if d.get("scheduled_at")
        else "🚀 فوري"
    )

    summary = (
        f"📋 <b>مراجعة الرسالة قبل الإرسال</b>\n\n"
        f"🎬 المحتوى: {type_icons.get(d.get('media_type', 'none'), '—')}\n"
        f"💵 الوصول: {paid_txt}\n"
        f"🔒 حماية: {'✅' if d.get('protect_content') else '❌'}\n"
        f"💣 التدمير: {dest_txt}\n"
        f"📡 الهدف: {target_labels.get(d.get('broadcast_target', 'users'), '—')}\n"
        f"⏰ الإرسال: {sched_txt}"
    )
    await msg_obj.reply_text(
        summary,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأكيد وإرسال", callback_data="confirm_create")],
            [InlineKeyboardButton("❌ إلغاء",         callback_data="wizard_cancel")],
        ]),
    )
    return MSG_CONFIRM


@admin_only
async def msg_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "wizard_cancel":
        context.user_data.clear()
        await query.message.edit_text(
            "❌ <b>تم إلغاء العملية.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 العودة للقائمة", callback_data="go_menu")
            ]]),
        )
        return ADMIN_MENU

    d = context.user_data["msg"]
    mtype_map = {
        "none": MediaType.NONE, "photo": MediaType.PHOTO, "video": MediaType.VIDEO,
        "audio": MediaType.AUDIO, "document": MediaType.DOCUMENT,
    }
    dest_map = {"never": DestructMode.NEVER, "after_time": DestructMode.AFTER_TIME}
    target   = d.get("broadcast_target", "users")

    with get_session() as session:
        msg_obj = db.create_broadcast_message(
            session,
            title           = None,
            text            = d.get("text"),
            media_type      = mtype_map.get(d.get("media_type", "none"), MediaType.NONE),
            media_file_id   = d.get("media_file_id"),
            is_paid         = d.get("is_paid", False),
            stars_price     = d.get("stars_price", 0),
            protect_content = d.get("protect_content", True),
            destruct_mode   = dest_map.get(d.get("destruct_mode", "never"), DestructMode.NEVER),
            destruct_seconds= d.get("destruct_seconds", 0),
            scheduled_at    = d.get("scheduled_at"),
            interval_hours  = d.get("interval_hours"),
            status          = MessageStatus.DRAFT,
            created_by      = update.effective_user.id,
        )
        msg_id = msg_obj.id

    if d.get("schedule_choice") == "sched_now":
        await query.message.edit_text(
            "⏳ <b>جارٍ البث...</b>",
            parse_mode="HTML",
        )
        stats = await broadcast(context.bot, msg_id, target=target)
        await query.message.reply_text(
            f"✅ <b>اكتمل البث!</b>\n\n"
            f"👥 المشتركون: {stats.get('users_total', 0)}\n"
            f"💬 المجموعات: {stats.get('groups_total', 0)}\n"
            f"✅ نجح: {stats.get('success', 0)}\n"
            f"❌ فشل: {stats.get('failed', 0)}",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )
    else:
        with get_session() as session:
            db.update_message_status(session, msg_id, MessageStatus.SCHEDULED)
        sched.schedule_broadcast(
            context.bot, msg_id,
            run_at        = d.get("scheduled_at"),
            interval_hours= d.get("interval_hours"),
            target        = target,
        )
        await query.message.edit_text(
            f"✅ <b>تمت جدولة الرسالة #{msg_id}</b>",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )

    context.user_data.clear()
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════════════════════

async def _show_stats(query):
    with get_session() as session:
        user_count   = db.get_user_count(session)
        group_count  = db.get_group_count(session)
        total_stars  = db.get_total_stars_earned(session)
        all_msgs     = db.get_all_messages(session)
        sent_count   = sum(1 for m in all_msgs if m.status == MessageStatus.SENT)
        sched_count  = sum(1 for m in all_msgs if m.status == MessageStatus.SCHEDULED)
        draft_count  = sum(1 for m in all_msgs if m.status == MessageStatus.DRAFT)

    await query.message.reply_text(
        f"📊 <b>الإحصائيات</b>\n\n"
        f"👥 المشتركون النشطون: <b>{user_count}</b>\n"
        f"💬 المجموعات النشطة: <b>{group_count}</b>\n"
        f"⭐ إجمالي النجوم: <b>{total_stars}</b>\n\n"
        f"📨 رسائل أُرسلت: <b>{sent_count}</b>\n"
        f"⏰ مجدولة: <b>{sched_count}</b>\n"
        f"📝 مسودات: <b>{draft_count}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")
        ]]),
    )
    return ADMIN_MENU


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE LIST & MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

async def _show_messages_list(query, context):
    with get_session() as session:
        raw_msgs = db.get_all_messages(session)
        msgs = [
            {"id": m.id, "title": m.title or f"رسالة #{m.id}",
             "status": m.status.value, "is_paid": m.is_paid,
             "media_type": m.media_type.value}
            for m in raw_msgs[:10]
        ]

    if not msgs:
        await query.message.reply_text(
            "📭 لا توجد رسائل بعد.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")
            ]]),
        )
        return ADMIN_MENU

    status_icons = {"draft": "📝", "scheduled": "⏰", "sent": "✅", "cancelled": "❌"}
    media_icons  = {"none": "📝", "photo": "🖼️", "video": "🎥", "audio": "🎵", "document": "📎"}

    rows = [
        [InlineKeyboardButton(
            f"{status_icons.get(m['status'], '❓')}"
            f"{media_icons.get(m['media_type'], '📄')}"
            f"{'💰' if m['is_paid'] else '🆓'} "
            f"[{m['id']}] {m['title']}",
            callback_data=f"mgr_{m['id']}"
        )]
        for m in msgs
    ]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")])

    await query.message.reply_text(
        "📋 <b>الرسائل (آخر 10):</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return BROADCAST_MANAGE


@admin_only
async def manage_msg_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data in ("admin_manage", "admin_back"):
        return await _show_messages_list(query, context)

    msg_id = int(query.data.split("_")[1])

    with get_session() as session:
        msg = db.get_message_by_id(session, msg_id)
        if not msg:
            await query.message.reply_text("❌ الرسالة غير موجودة.")
            return ADMIN_MENU

        d = {
            "id":           msg.id,
            "media_type":   msg.media_type.value,
            "is_paid":      msg.is_paid,
            "stars_price":  msg.stars_price,
            "protect":      msg.protect_content,
            "dest_mode":    msg.destruct_mode.value,
            "dest_secs":    msg.destruct_seconds,
            "status":       msg.status.value,
            "created_at":   msg.created_at.strftime("%Y-%m-%d %H:%M"),
            "sent_at":      msg.sent_at.strftime("%Y-%m-%d %H:%M") if msg.sent_at else "—",
            "interval":     f"كل {msg.interval_hours}h" if msg.interval_hours else "—",
        }

    media_icons = {"none": "📝", "photo": "🖼️", "video": "🎥", "audio": "🎵", "document": "📎"}
    paid_txt    = f"💰 {d['stars_price']} ⭐" if d["is_paid"] else "🆓 مجاني"
    dest_txt    = "🔴 لا تُحذف" if d["dest_mode"] == "never" else f"⏱️ بعد {d['dest_secs']}ث"
    status_icons= {"draft": "📝", "scheduled": "⏰", "sent": "✅", "cancelled": "❌"}

    detail = (
        f"📨 <b>رسالة #{d['id']}</b>\n\n"
        f"🎬 النوع: {media_icons.get(d['media_type'], '📄')}\n"
        f"💵 الوصول: {paid_txt}\n"
        f"🔒 حماية: {'✅' if d['protect'] else '❌'}\n"
        f"💣 التدمير: {dest_txt}\n"
        f"📅 الحالة: {status_icons.get(d['status'], '❓')} {d['status']}\n"
        f"🔁 التكرار: {d['interval']}\n"
        f"🕐 أُنشئت: {d['created_at']}\n"
        f"📤 أُرسلت: {d['sent_at']}"
    )

    await query.message.edit_text(
        detail,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 إرسال الآن", callback_data=f"send_now_{msg_id}"),
             InlineKeyboardButton("🗑️ حذف",       callback_data=f"del_msg_{msg_id}")],
            [InlineKeyboardButton("🔙 القائمة",    callback_data="admin_manage"),
             InlineKeyboardButton("❌ إغلاق",      callback_data="admin_back")],
        ]),
    )
    return BROADCAST_MANAGE


@admin_only
async def send_now_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer("⏳ جارٍ البث...")
    msg_id = int(query.data.split("_")[2])

    await query.message.edit_text("⏳ <b>جارٍ البث...</b>", parse_mode="HTML")
    stats = await broadcast(context.bot, msg_id, target="users")

    await query.message.edit_text(
        f"✅ <b>اكتمل البث!</b>\n\n"
        f"👥 الإجمالي: {stats.get('total', 0)}\n"
        f"✅ نجح: {stats.get('success', 0)}\n"
        f"❌ فشل: {stats.get('failed', 0)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 القائمة", callback_data="admin_manage"),
             InlineKeyboardButton("🏠 الرئيسية", callback_data="admin_back")],
        ]),
    )
    return BROADCAST_MANAGE


@admin_only
async def del_msg_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    msg_id = int(query.data.split("_")[2])

    sched.cancel_broadcast(msg_id)
    with get_session() as session:
        db.delete_message_record(session, msg_id)

    await query.message.edit_text(
        f"🗑️ تم حذف الرسالة #{msg_id}.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 القائمة", callback_data="admin_manage"),
             InlineKeyboardButton("🏠 الرئيسية", callback_data="admin_back")],
        ]),
    )
    return BROADCAST_MANAGE


# ═══════════════════════════════════════════════════════════════════════════════
# INVITE LINKS
# ═══════════════════════════════════════════════════════════════════════════════

async def _show_invite_menu(query, context):
    await query.message.reply_text(
        "🔗 <b>روابط الدعوة</b>\n\nالروابط للمشرفين فقط.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 رابطي الشخصي",    callback_data="invite_my")],
            [InlineKeyboardButton("✨ توليد رابط جديد",  callback_data="invite_new")],
            [InlineKeyboardButton("🔙 رجوع",             callback_data="admin_back")],
        ]),
    )
    return INVITE_MENU


@admin_only
async def invite_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    from bot.services.referral import get_or_create_user_invite, build_invite_url

    if query.data == "invite_my":
        token = get_or_create_user_invite(update.effective_user.id)
        url   = build_invite_url(token)
        await query.message.reply_text(
            f"🔗 <b>رابطك الشخصي:</b>\n<code>{url}</code>",
            parse_mode="HTML",
        )
        return INVITE_MENU

    if query.data == "invite_new":
        with get_session() as session:
            link  = db.create_invite_link(session, update.effective_user.id)
            token = link.token
        url = build_invite_url(token)
        await query.message.reply_text(
            f"✨ <b>رابط جديد:</b>\n<code>{url}</code>",
            parse_mode="HTML",
        )
        return INVITE_MENU

    if query.data == "admin_back":
        return await admin_menu_cb(update, context)

    return INVITE_MENU


# ═══════════════════════════════════════════════════════════════════════════════
# BOT SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

async def _show_settings_menu(query, context):
    with get_session() as session:
        enabled  = db.get_setting(session, "welcome_enabled", "true")
        text_raw = db.get_setting(session, "welcome_text", "(افتراضية)")

    status  = "✅ مفعّلة" if enabled.lower() == "true" else "❌ معطّلة"
    preview = text_raw[:100] + "..." if len(text_raw) > 100 else text_raw

    await query.message.reply_text(
        f"⚙️ <b>إعدادات البوت</b>\n\n"
        f"📩 رسالة الترحيب: <b>{status}</b>\n"
        f"📝 المعاينة:\n<i>{preview}</i>\n\n"
        "💡 استخدم <code>{name}</code> لاسم المستخدم.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ تعديل نص الترحيب",       callback_data="set_welcome_text")],
            [InlineKeyboardButton("🔄 تفعيل / تعطيل الترحيب",  callback_data="set_welcome_toggle")],
            [InlineKeyboardButton("🔙 رجوع",                    callback_data="admin_back")],
        ]),
    )
    return SETTINGS_MENU


@admin_only
async def settings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "set_welcome_text":
        await query.message.reply_text(
            "✏️ أرسل نص رسالة الترحيب الجديدة:\n\n"
            "💡 <code>{name}</code> = اسم المستخدم\n"
            "✅ يدعم HTML\n"
            "أرسل <code>NONE</code> لتعطيل الترحيب.",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return WELCOME_TEXT_INPUT

    if data == "set_welcome_toggle":
        with get_session() as session:
            current = db.get_setting(session, "welcome_enabled", "true")
            new_val = "false" if current.lower() == "true" else "true"
            db.set_setting(session, "welcome_enabled", new_val)
        status = "✅ مفعّلة" if new_val == "true" else "❌ معطّلة"
        await query.message.reply_text(
            f"تم تغيير حالة الترحيب إلى: <b>{status}</b>",
            parse_mode="HTML", reply_markup=_admin_menu_kb(),
        )
        return ADMIN_MENU

    if data == "admin_back":
        return await admin_menu_cb(update, context)
    return SETTINGS_MENU


@admin_only
async def welcome_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    with get_session() as session:
        if text.upper() == "NONE":
            db.set_setting(session, "welcome_enabled", "false")
            await update.message.reply_text("✅ تم تعطيل رسالة الترحيب.", reply_markup=_admin_menu_kb())
        else:
            db.set_setting(session, "welcome_text", text)
            db.set_setting(session, "welcome_enabled", "true")
            await update.message.reply_text(
                f"✅ تم حفظ رسالة الترحيب.", reply_markup=_admin_menu_kb()
            )
    return ADMIN_MENU


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

async def _show_admins_menu(query, context):
    with get_session() as session:
        dynamic  = db.list_admins(session)
        dyn_list = [(a.telegram_id, a.note or "") for a in dynamic]

    static_lines = "\n".join(f"  • <code>{uid}</code>" for uid in config._STATIC_ADMIN_IDS) or "  —"
    dyn_lines    = "\n".join(f"  • <code>{uid}</code> {n}" for uid, n in dyn_list) or "  لا يوجد"

    await query.message.reply_text(
        f"👑 <b>المشرفون</b>\n\n"
        f"📌 ثابتون:\n{static_lines}\n\n"
        f"➕ ديناميكيون:\n{dyn_lines}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ إضافة مشرف",  callback_data="admins_add")],
            [InlineKeyboardButton("➖ إزالة مشرف",  callback_data="admins_remove")],
            [InlineKeyboardButton("🔙 رجوع",         callback_data="admin_back")],
        ]),
    )
    return SETTINGS_MENU


@admin_only
async def admins_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "admins_add":
        await query.message.reply_text(
            "➕ أرسل معرف المستخدم الذي تريد ترقيته مشرفاً:\n\n"
            "أو أرسل /cancel للإلغاء",
            reply_markup=_cancel_kb(),
        )
        return ADD_ADMIN_INPUT

    if query.data == "admins_remove":
        await query.message.reply_text(
            "➖ أرسل معرف المشرف الذي تريد إزالته:\n\n"
            "أو أرسل /cancel للإلغاء",
            reply_markup=_cancel_kb(),
        )
        return REMOVE_ADMIN_INPUT

    if query.data == "admin_back":
        return await admin_menu_cb(update, context)
    return SETTINGS_MENU


@admin_only
async def add_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split(maxsplit=1)
    try:
        target_id = int(parts[0])
    except ValueError:
        await update.message.reply_text("❌ معرف غير صالح.", reply_markup=_cancel_kb())
        return ADD_ADMIN_INPUT

    note = parts[1] if len(parts) > 1 else ""
    with get_session() as session:
        ok = db.add_admin(session, target_id, update.effective_user.id, note)

    if ok:
        await update.message.reply_text(
            f"✅ تمت إضافة <code>{target_id}</code> مشرفاً.",
            parse_mode="HTML", reply_markup=_admin_menu_kb(),
        )
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="👑 تهانينا! تمت ترقيتك مشرفاً.\nاستخدم /admin للوصول للوحة التحكم.",
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(
            f"⚠️ <code>{target_id}</code> مشرف بالفعل.",
            parse_mode="HTML", reply_markup=_admin_menu_kb(),
        )
    return ADMIN_MENU


@admin_only
async def remove_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ معرف غير صالح.", reply_markup=_cancel_kb())
        return REMOVE_ADMIN_INPUT

    if target_id in config._STATIC_ADMIN_IDS:
        await update.message.reply_text(
            "⚠️ لا يمكن إزالة مشرف ثابت (محدد في .env).",
            reply_markup=_admin_menu_kb(),
        )
        return ADMIN_MENU

    with get_session() as session:
        ok = db.remove_admin(session, target_id)

    msg_text = f"✅ تمت إزالة <code>{target_id}</code>." if ok else f"❌ <code>{target_id}</code> غير موجود."
    await update.message.reply_text(msg_text, parse_mode="HTML", reply_markup=_admin_menu_kb())
    return ADMIN_MENU


# ═══════════════════════════════════════════════════════════════════════════════
# BAN / UNBAN
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def ban_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("ban_action", "ban")
    try:
        target_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ أدخل رقماً.", reply_markup=_cancel_kb())
        return BAN_INPUT

    with get_session() as session:
        if action == "ban":
            ok  = db.ban_user(session, target_id)
            txt = f"✅ تم حظر <code>{target_id}</code>" if ok else "❌ المستخدم غير موجود."
        else:
            ok  = db.unban_user(session, target_id)
            txt = f"✅ تم رفع الحظر عن <code>{target_id}</code>" if ok else "❌ المستخدم غير موجود."

    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=_admin_menu_kb())
    return ADMIN_MENU


# ═══════════════════════════════════════════════════════════════════════════════
# CANCEL COMMAND
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ تم الإلغاء.", reply_markup=_admin_menu_kb())
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# REGISTER
# ═══════════════════════════════════════════════════════════════════════════════

def register_admin_handlers(app):
    conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_start)],
        states={
            ADMIN_MENU: [
                CallbackQueryHandler(
                    admin_menu_cb,
                    pattern=(
                        "^(admin_new_msg|admin_manage|admin_stats|admin_invite"
                        "|admin_settings|admin_admins|admin_ban|admin_unban"
                        "|admin_back|go_menu)$"
                    ),
                ),
            ],
            MSG_CONTENT: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.AUDIO
                     | filters.VOICE | filters.Document.ALL) & ~filters.COMMAND,
                    msg_content,
                ),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
            MSG_PAID_CHOICE: [
                CallbackQueryHandler(msg_paid_choice,    pattern=r"^paid_"),
                CallbackQueryHandler(wizard_cancel,      pattern="^wizard_cancel$"),
            ],
            MSG_STARS_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_stars_amount),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
            MSG_PROTECT: [
                CallbackQueryHandler(msg_protect, pattern=r"^protect_"),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
            MSG_DESTRUCT_SECONDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_destruct_seconds),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
            MSG_BROADCAST_TARGET: [
                CallbackQueryHandler(msg_broadcast_target, pattern=r"^target_"),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
            MSG_SCHEDULE_CHOICE: [
                CallbackQueryHandler(msg_schedule_choice, pattern=r"^sched_"),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
            MSG_SCHEDULE_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_schedule_time),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
            MSG_INTERVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_interval),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
            MSG_CONFIRM: [
                CallbackQueryHandler(msg_confirm, pattern=r"^(confirm_create|wizard_cancel)$"),
            ],
            BROADCAST_MANAGE: [
                CallbackQueryHandler(manage_msg_cb, pattern=r"^mgr_\d+$"),
                CallbackQueryHandler(send_now_cb,   pattern=r"^send_now_\d+$"),
                CallbackQueryHandler(del_msg_cb,    pattern=r"^del_msg_\d+$"),
                CallbackQueryHandler(manage_msg_cb, pattern="^admin_(manage|back)$"),
                CallbackQueryHandler(go_menu_cb,    pattern="^go_menu$"),
            ],
            INVITE_MENU: [
                CallbackQueryHandler(invite_cb, pattern="^(invite_my|invite_new|admin_back)$"),
            ],
            SETTINGS_MENU: [
                CallbackQueryHandler(settings_cb,  pattern="^(set_welcome_text|set_welcome_toggle|admin_back)$"),
                CallbackQueryHandler(admins_cb,    pattern="^(admins_add|admins_remove|admin_back)$"),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
            WELCOME_TEXT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, welcome_text_input),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
            ADD_ADMIN_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_input),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
            REMOVE_ADMIN_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, remove_admin_input),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
            BAN_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ban_input),
                CallbackQueryHandler(wizard_cancel, pattern="^wizard_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("admin",  admin_start),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    app.add_handler(conv)
    # go_menu outside conversation (after wizard_cancel)
    app.add_handler(CallbackQueryHandler(go_menu_cb, pattern="^go_menu$"))
