"""
Admin control panel — full conversation-based wizard.

New in this version:
  • Dynamic admin management (add / remove / list)
  • Bot settings: customize or disable the welcome message
  • Invite links are admin-only (removed from user panel)
  • Uses config.is_admin() for dynamic admin checking
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
    BAN_INPUT,
    SETTINGS_MENU,
    WELCOME_TEXT_INPUT,
    ADD_ADMIN_INPUT,
    REMOVE_ADMIN_INPUT,
    INVITE_MENU,
) = range(21)

# ─── Admin guard ──────────────────────────────────────────────────────────────

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not config.is_admin(user_id):
            if update.message:
                await update.message.reply_text("🚫 هذا الأمر للمشرفين فقط.")
            elif update.callback_query:
                await update.callback_query.answer("🚫 غير مصرح لك.", show_alert=True)
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapper


# ─── Main menu ────────────────────────────────────────────────────────────────

def _admin_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 إنشاء رسالة جديدة",     callback_data="admin_new_msg")],
        [InlineKeyboardButton("📋 إدارة الرسائل",          callback_data="admin_manage")],
        [InlineKeyboardButton("📊 الإحصائيات",             callback_data="admin_stats")],
        [InlineKeyboardButton("🔗 توليد رابط دعوة",        callback_data="admin_invite")],
        [InlineKeyboardButton("⚙️ إعدادات البوت",          callback_data="admin_settings")],
        [InlineKeyboardButton("👑 إدارة المشرفين",         callback_data="admin_admins")],
        [InlineKeyboardButton("🚫 حظر",  callback_data="admin_ban"),
         InlineKeyboardButton("✅ رفع حظر", callback_data="admin_unban")],
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


# ─── ADMIN_MENU callbacks ─────────────────────────────────────────────────────

@admin_only
async def admin_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── New message ───────────────────────────────────────────────────────────
    if data == "admin_new_msg":
        context.user_data["new_msg"] = {}
        await query.message.reply_text(
            "📝 <b>خطوة 1/9 — العنوان الداخلي</b>\n\nأدخل عنواناً وصفياً للرسالة:",
            parse_mode="HTML",
        )
        return MSG_TITLE

    # ── Manage messages ───────────────────────────────────────────────────────
    elif data == "admin_manage":
        return await _show_messages_list(query, context)

    # ── Stats ─────────────────────────────────────────────────────────────────
    elif data == "admin_stats":
        return await _show_stats(query)

    # ── Invite link ───────────────────────────────────────────────────────────
    elif data == "admin_invite":
        return await _show_invite_menu(query, context)

    # ── Settings ──────────────────────────────────────────────────────────────
    elif data == "admin_settings":
        return await _show_settings(query, context)

    # ── Admins ────────────────────────────────────────────────────────────────
    elif data == "admin_admins":
        return await _show_admins(query, context)

    # ── Ban ───────────────────────────────────────────────────────────────────
    elif data == "admin_ban":
        await query.message.reply_text("🚫 أدخل معرف المستخدم (Telegram ID) لحظره:")
        context.user_data["ban_action"] = "ban"
        return BAN_INPUT

    elif data == "admin_unban":
        await query.message.reply_text("✅ أدخل معرف المستخدم لرفع الحظر:")
        context.user_data["ban_action"] = "unban"
        return BAN_INPUT

    elif data == "admin_back":
        await query.message.edit_text(
            "🛠️ <b>لوحة التحكم</b>\n\nاختر الإجراء:",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )
        return ADMIN_MENU


# ═══════════════════════════════════════════════════════════════════════════════
# INVITE LINKS  (admin-only)
# ═══════════════════════════════════════════════════════════════════════════════

async def _show_invite_menu(query, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 رابطي الشخصي (كأدمن)", callback_data="invite_my")],
        [InlineKeyboardButton("✨ توليد رابط جديد",        callback_data="invite_new")],
        [InlineKeyboardButton("🔙 رجوع",                   callback_data="admin_back")],
    ])
    await query.message.reply_text(
        "🔗 <b>إدارة روابط الدعوة</b>\n\n"
        "روابط الدعوة للمشرفين فقط — المستخدمون لا يستطيعون توليد روابط.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return INVITE_MENU


@admin_only
async def invite_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    from bot.services.referral import get_or_create_user_invite, build_invite_url
    import secrets
    from database import db as _db

    if data == "invite_my":
        token = get_or_create_user_invite(update.effective_user.id)
        url = build_invite_url(token)
        await query.message.reply_text(
            f"🔗 <b>رابط دعوتك:</b>\n<code>{url}</code>",
            parse_mode="HTML",
        )
    elif data == "invite_new":
        # Generate a fresh unique link
        with get_session() as session:
            link = _db.create_invite_link(session, update.effective_user.id)
        url = build_invite_url(link.token)
        await query.message.reply_text(
            f"✨ <b>رابط دعوة جديد:</b>\n<code>{url}</code>",
            parse_mode="HTML",
        )
    elif data == "admin_back":
        await query.message.edit_text(
            "🛠️ <b>لوحة التحكم</b>",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )
        return ADMIN_MENU

    return INVITE_MENU


# ═══════════════════════════════════════════════════════════════════════════════
# BOT SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

async def _show_settings(query, context):
    with get_session() as session:
        enabled = db.get_setting(session, "welcome_enabled", "true")
        text_preview = db.get_setting(session, "welcome_text", "(افتراضية)")

    status = "✅ مفعّلة" if enabled.lower() == "true" else "❌ معطّلة"
    preview = text_preview[:80] + "..." if len(text_preview) > 80 else text_preview

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ تعديل نص الترحيب",       callback_data="set_welcome_text")],
        [InlineKeyboardButton("🔄 تفعيل/تعطيل الترحيب",   callback_data="set_welcome_toggle")],
        [InlineKeyboardButton("🔙 رجوع",                    callback_data="admin_back")],
    ])
    await query.message.reply_text(
        f"⚙️ <b>إعدادات البوت</b>\n\n"
        f"📩 رسالة الترحيب: <b>{status}</b>\n"
        f"📝 المعاينة:\n<i>{preview}</i>\n\n"
        "💡 يمكنك استخدام <code>{name}</code> في النص ليُستبدل باسم المستخدم.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return SETTINGS_MENU


@admin_only
async def settings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "set_welcome_text":
        await query.message.reply_text(
            "✏️ أرسل نص رسالة الترحيب الجديدة.\n\n"
            "💡 استخدم <code>{name}</code> لاسم المستخدم، و<code>{id}</code> لمعرفه.\n"
            "✅ يدعم HTML.\n"
            "أو أرسل كلمة <code>none</code> لإزالة الرسالة الترحيبية.",
            parse_mode="HTML",
        )
        return WELCOME_TEXT_INPUT

    elif data == "set_welcome_toggle":
        with get_session() as session:
            current = db.get_setting(session, "welcome_enabled", "true")
            new_val = "false" if current.lower() == "true" else "true"
            db.set_setting(session, "welcome_enabled", new_val)
        status = "✅ مفعّلة" if new_val == "true" else "❌ معطّلة"
        await query.message.reply_text(
            f"✅ تم تغيير حالة رسالة الترحيب إلى: <b>{status}</b>",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )
        return ADMIN_MENU

    elif data == "admin_back":
        await query.message.edit_text(
            "🛠️ <b>لوحة التحكم</b>",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )
        return ADMIN_MENU


@admin_only
async def welcome_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    with get_session() as session:
        if text.lower() == "none":
            db.set_setting(session, "welcome_enabled", "false")
            await update.message.reply_text(
                "✅ تم تعطيل رسالة الترحيب.",
                reply_markup=_admin_menu_kb(),
            )
        else:
            db.set_setting(session, "welcome_text", text)
            db.set_setting(session, "welcome_enabled", "true")
            await update.message.reply_text(
                f"✅ تم حفظ رسالة الترحيب:\n\n{text}",
                parse_mode="HTML",
                reply_markup=_admin_menu_kb(),
            )
    return ADMIN_MENU


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

async def _show_admins(query, context):
    with get_session() as session:
        dynamic = db.list_admins(session)

    static_ids = config._STATIC_ADMIN_IDS
    lines = [f"📌 <b>أدمن ثابت (من .env):</b>"]
    for uid in static_ids:
        lines.append(f"  • <code>{uid}</code>")

    if dynamic:
        lines.append("\n👑 <b>أدمن مضاف ديناميكياً:</b>")
        for a in dynamic:
            note = f" — {a.note}" if a.note else ""
            lines.append(f"  • <code>{a.telegram_id}</code>{note}")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ إضافة أدمن",   callback_data="admins_add")],
        [InlineKeyboardButton("➖ إزالة أدمن",   callback_data="admins_remove")],
        [InlineKeyboardButton("🔙 رجوع",          callback_data="admin_back")],
    ])
    await query.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return SETTINGS_MENU   # reuse state; differentiate by callback pattern


@admin_only
async def admins_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "admins_add":
        await query.message.reply_text(
            "➕ أرسل معرف المستخدم (Telegram ID) الذي تريد ترقيته إلى أدمن:\n"
            "(يمكنك إضافة ملاحظة بعد المعرف مفصولة بمسافة)"
        )
        context.user_data["admin_action"] = "add"
        return ADD_ADMIN_INPUT

    elif data == "admins_remove":
        await query.message.reply_text(
            "➖ أرسل معرف الأدمن الذي تريد إزالته:"
        )
        context.user_data["admin_action"] = "remove"
        return REMOVE_ADMIN_INPUT

    elif data == "admin_back":
        await query.message.edit_text(
            "🛠️ <b>لوحة التحكم</b>",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )
        return ADMIN_MENU


@admin_only
async def add_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split(maxsplit=1)
    try:
        target_id = int(parts[0])
    except ValueError:
        await update.message.reply_text("❌ معرف غير صالح. أرسل رقماً فقط.")
        return ADD_ADMIN_INPUT

    note = parts[1] if len(parts) > 1 else ""
    with get_session() as session:
        success = db.add_admin(session, target_id, update.effective_user.id, note)

    if success:
        await update.message.reply_text(
            f"✅ تمت إضافة <code>{target_id}</code> كمشرف.",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )
        # Notify the newly added admin
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="👑 تهانينا! تمت ترقيتك إلى مشرف في البوت.\nاستخدم /admin للوصول إلى لوحة التحكم.",
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(
            f"⚠️ المستخدم <code>{target_id}</code> مشرف بالفعل.",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )
    return ADMIN_MENU


@admin_only
async def remove_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        target_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ معرف غير صالح.")
        return REMOVE_ADMIN_INPUT

    # Cannot remove static admins
    if target_id in config._STATIC_ADMIN_IDS:
        await update.message.reply_text(
            "⚠️ لا يمكن إزالة أدمن ثابت (محدد في ملف .env).",
            reply_markup=_admin_menu_kb(),
        )
        return ADMIN_MENU

    with get_session() as session:
        success = db.remove_admin(session, target_id)

    if success:
        await update.message.reply_text(
            f"✅ تمت إزالة <code>{target_id}</code> من المشرفين.",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )
    else:
        await update.message.reply_text(
            f"❌ المستخدم <code>{target_id}</code> غير موجود في قائمة المشرفين الديناميكيين.",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )
    return ADMIN_MENU


# ═══════════════════════════════════════════════════════════════════════════════
# BAN / UNBAN
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def ban_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    action = context.user_data.get("ban_action", "ban")
    try:
        target_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ معرف غير صالح. أدخل رقماً.")
        return BAN_INPUT

    with get_session() as session:
        if action == "ban":
            ok = db.ban_user(session, target_id)
            msg = f"✅ تم حظر <code>{target_id}</code>" if ok else "❌ المستخدم غير موجود."
        else:
            ok = db.unban_user(session, target_id)
            msg = f"✅ تم رفع الحظر عن <code>{target_id}</code>" if ok else "❌ المستخدم غير موجود."

    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=_admin_menu_kb())
    return ADMIN_MENU


# ═══════════════════════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════════════════════

async def _show_stats(query):
    with get_session() as session:
        user_count  = db.get_user_count(session)
        total_stars = db.get_total_stars_earned(session)
        sent_count  = len(db.get_all_messages(session, status=MessageStatus.SENT))
        sched_count = len(db.get_all_messages(session, status=MessageStatus.SCHEDULED))

    await query.message.reply_text(
        f"📊 <b>الإحصائيات</b>\n\n"
        f"👥 المشتركون النشطون: <b>{user_count}</b>\n"
        f"⭐ إجمالي النجوم المكتسبة: <b>{total_stars}</b>\n"
        f"📨 الرسائل المُرسلة: <b>{sent_count}</b>\n"
        f"⏰ الرسائل المجدولة: <b>{sched_count}</b>",
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
        messages = db.get_all_messages(session)

    if not messages:
        await query.message.reply_text(
            "📭 لا توجد رسائل.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")
            ]]),
        )
        return ADMIN_MENU

    rows = []
    for m in messages[:10]:
        icons = {
            "draft": "📝", "scheduled": "⏰", "sent": "✅", "cancelled": "❌"
        }
        s = icons.get(m.status.value, "❓")
        p = "💰" if m.is_paid else "🆓"
        rows.append([InlineKeyboardButton(
            f"{s}{p} [{m.id}] {m.title or 'بدون عنوان'}",
            callback_data=f"mgr_{m.id}"
        )])
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
    msg_id = int(query.data.split("_")[1])

    with get_session() as session:
        msg = db.get_message_by_id(session, msg_id)
        if not msg:
            await query.message.reply_text("❌ الرسالة غير موجودة.")
            return ADMIN_MENU

        paid_txt = f"💰 {msg.stars_price} ⭐" if msg.is_paid else "🆓 مجاني"
        dm = {"never": "لا تُحذف", "immediate": "⚡ فوري",
              "after_time": f"⏱️ {msg.destruct_seconds}ث"}.get(msg.destruct_mode.value, "—")

        detail = (
            f"📨 <b>رسالة #{msg.id}</b>\n\n"
            f"📌 {msg.title or '—'}\n"
            f"🎬 {msg.media_type.value}\n"
            f"💵 {paid_txt}\n"
            f"🔒 حماية: {'✅' if msg.protect_content else '❌'}\n"
            f"💣 التدمير: {dm}\n"
            f"📅 الحالة: {msg.status.value}\n"
            f"🕐 أُنشئت: {msg.created_at.strftime('%Y-%m-%d %H:%M')}"
        )

    await query.message.edit_text(
        detail,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 إرسال الآن", callback_data=f"send_now_{msg_id}"),
             InlineKeyboardButton("🗑️ حذف",       callback_data=f"del_msg_{msg_id}")],
            [InlineKeyboardButton("🔙 القائمة",    callback_data="admin_manage")],
        ]),
    )
    return BROADCAST_MANAGE


@admin_only
async def send_now_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ جارٍ البث...")
    msg_id = int(query.data.split("_")[2])
    await query.message.edit_text("⏳ <b>جارٍ البث لجميع المشتركين...</b>", parse_mode="HTML")
    stats = await broadcast(context.bot, msg_id)
    await query.message.edit_text(
        f"✅ <b>اكتمل البث!</b>\n\n"
        f"📤 الإجمالي: {stats.get('total', 0)}\n"
        f"✅ نجح: {stats.get('success', 0)}\n"
        f"❌ فشل: {stats.get('failed', 0)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 القائمة", callback_data="admin_manage")
        ]]),
    )
    return BROADCAST_MANAGE


@admin_only
async def del_msg_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    msg_id = int(query.data.split("_")[2])
    sched.cancel_broadcast(msg_id)
    with get_session() as session:
        db.delete_message_record(session, msg_id)
    await query.message.edit_text(
        f"🗑️ تم حذف الرسالة #{msg_id}.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 القائمة", callback_data="admin_manage")
        ]]),
    )
    return BROADCAST_MANAGE


# ═══════════════════════════════════════════════════════════════════════════════
# NEW MESSAGE WIZARD  (9 steps)
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def msg_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_msg"]["title"] = update.message.text.strip()
    await update.message.reply_text(
        "📂 <b>خطوة 2/9 — نوع الوسائط</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 نص فقط",   callback_data="mtype_none"),
             InlineKeyboardButton("🖼️ صورة",     callback_data="mtype_photo")],
            [InlineKeyboardButton("🎥 فيديو",    callback_data="mtype_video"),
             InlineKeyboardButton("🎵 صوت",      callback_data="mtype_audio")],
            [InlineKeyboardButton("📎 ملف",      callback_data="mtype_document")],
        ]),
    )
    return MSG_MEDIA_TYPE


@admin_only
async def msg_media_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mtype = query.data.split("_")[1]
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
            f"أرسل ملف <b>{mtype}</b> الآن. (يمكنك إضافة نص caption معه)",
            parse_mode="HTML",
        )
        return MSG_MEDIA_UPLOAD


@admin_only
async def msg_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_msg"]["text"] = update.message.text
    context.user_data["new_msg"]["media_file_id"] = None
    return await _ask_paid(update, context)


@admin_only
async def msg_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mtype = context.user_data["new_msg"]["media_type"]
    file_id = caption = None

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
    await update.message.reply_text(
        "💵 <b>خطوة 4/9 — نوع الوصول</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🆓 مجاني (للجميع)",        callback_data="paid_no")],
            [InlineKeyboardButton("💰 مدفوع (نجوم ⭐)",       callback_data="paid_yes")],
        ]),
    )
    return MSG_PAID_CHOICE


@admin_only
async def msg_paid_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    is_paid = (query.data == "paid_yes")
    context.user_data["new_msg"]["is_paid"] = is_paid

    if is_paid:
        await query.message.reply_text(
            "⭐ <b>خطوة 5/9 — سعر النجوم</b>\n\nأدخل عدد النجوم (مثال: 50):",
            parse_mode="HTML",
        )
        return MSG_STARS_AMOUNT
    else:
        context.user_data["new_msg"]["stars_price"] = 0
        return await _ask_protect_q(query, context)


@admin_only
async def msg_stars_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        stars = int(update.message.text.strip())
        assert stars >= 1
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ أدخل رقماً أكبر من صفر.")
        return MSG_STARS_AMOUNT
    context.user_data["new_msg"]["stars_price"] = stars
    return await _ask_protect_q(update, context)


async def _ask_protect_q(obj, context):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 نعم — منع التوجيه والنسخ", callback_data="protect_yes")],
        [InlineKeyboardButton("🔓 لا — السماح بالتوجيه",    callback_data="protect_no")],
    ])
    text = (
        "🔐 <b>خطوة 6/9 — حماية المحتوى</b>\n\n"
        "منع التوجيه (Forward) والنسخ؟\n"
        "<i>لا يمنع لقطة الشاشة — قيد تليجرام.</i>"
    )
    if hasattr(obj, "message"):
        await obj.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return MSG_PROTECT


@admin_only
async def msg_protect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_msg"]["protect_content"] = (query.data == "protect_yes")
    await query.message.reply_text(
        "💣 <b>خطوة 7/9 — ذاتية التدمير</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔴 لا تُحذف أبداً",         callback_data="dest_never")],
            [InlineKeyboardButton("⚡ حذف فوري بعد المشاهدة",  callback_data="dest_immediate")],
            [InlineKeyboardButton("⏱️ حذف بعد فترة زمنية",    callback_data="dest_after_time")],
        ]),
    )
    return MSG_DESTRUCT_MODE


@admin_only
async def msg_destruct_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split("_", 1)[1]   # never / immediate / after_time
    context.user_data["new_msg"]["destruct_mode"] = mode

    if mode == "after_time":
        await query.message.reply_text(
            "⏱️ <b>خطوة 7b — مدة التدمير</b>\n\nأدخل عدد الثواني (مثال: 60):",
            parse_mode="HTML",
        )
        return MSG_DESTRUCT_TIME
    else:
        context.user_data["new_msg"]["destruct_seconds"] = 0
        return await _ask_schedule_q(query, context)


@admin_only
async def msg_destruct_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        secs = int(update.message.text.strip())
        assert secs >= 1
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ أدخل رقماً أكبر من صفر.")
        return MSG_DESTRUCT_TIME
    context.user_data["new_msg"]["destruct_seconds"] = secs
    return await _ask_schedule_q(update, context)


async def _ask_schedule_q(obj, context):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 إرسال فوري",        callback_data="sched_now")],
        [InlineKeyboardButton("📅 جدولة وقت محدد",   callback_data="sched_time")],
        [InlineKeyboardButton("🔁 إرسال متكرر",       callback_data="sched_repeat")],
    ])
    text = "⏰ <b>خطوة 8/9 — جدولة الإرسال</b>"
    if hasattr(obj, "message"):
        await obj.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return MSG_SCHEDULE_CHOICE


@admin_only
async def msg_schedule_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data
    context.user_data["new_msg"]["schedule_choice"] = choice

    if choice == "sched_now":
        context.user_data["new_msg"]["scheduled_at"] = None
        context.user_data["new_msg"]["interval_hours"] = None
        return await _show_confirm(query, context)
    elif choice == "sched_time":
        await query.message.reply_text(
            "📅 <b>خطوة 8b — وقت الإرسال</b>\n\n"
            "الصيغة: <code>YYYY-MM-DD HH:MM</code>\n"
            "مثال: <code>2025-12-31 20:00</code>",
            parse_mode="HTML",
        )
        return MSG_SCHEDULE_TIME
    elif choice == "sched_repeat":
        await query.message.reply_text(
            "🔁 <b>خطوة 8b — فترة التكرار</b>\n\nأدخل عدد الساعات (مثال: 24 = يومياً):",
            parse_mode="HTML",
        )
        return MSG_INTERVAL


@admin_only
async def msg_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        dt = datetime.datetime.strptime(update.message.text.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text("❌ الصيغة خاطئة. مثال: 2025-12-31 20:00")
        return MSG_SCHEDULE_TIME
    context.user_data["new_msg"]["scheduled_at"] = dt
    context.user_data["new_msg"]["interval_hours"] = None
    return await _show_confirm(update, context)


@admin_only
async def msg_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hours = float(update.message.text.strip())
        assert hours > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ أدخل رقماً أكبر من صفر.")
        return MSG_INTERVAL
    context.user_data["new_msg"]["interval_hours"] = hours
    context.user_data["new_msg"]["scheduled_at"] = None
    return await _show_confirm(update, context)


async def _show_confirm(obj, context):
    d = context.user_data["new_msg"]
    paid_txt = f"💰 {d.get('stars_price', 0)} ⭐" if d.get("is_paid") else "🆓 مجاني"
    dm_map = {
        "never": "لا تُحذف",
        "immediate": "⚡ فوري",
        "after_time": f"⏱️ بعد {d.get('destruct_seconds', 0)}ث",
    }
    sched_txt = (
        f"🔁 كل {d['interval_hours']} ساعة" if d.get("interval_hours")
        else str(d.get("scheduled_at")) if d.get("scheduled_at")
        else "🚀 فوري"
    )
    summary = (
        f"📋 <b>ملخص الرسالة — خطوة 9/9</b>\n\n"
        f"📌 العنوان: {d.get('title', '—')}\n"
        f"🎬 النوع: {d.get('media_type', 'none')}\n"
        f"💵 الوصول: {paid_txt}\n"
        f"🔒 حماية: {'✅' if d.get('protect_content') else '❌'}\n"
        f"💣 التدمير: {dm_map.get(d.get('destruct_mode', 'never'), '—')}\n"
        f"⏰ الإرسال: {sched_txt}\n\n"
        "هل تريد تأكيد الإنشاء؟"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد وإنشاء", callback_data="confirm_create"),
         InlineKeyboardButton("❌ إلغاء",         callback_data="cancel_create")],
    ])
    if hasattr(obj, "message"):
        await obj.message.reply_text(summary, parse_mode="HTML", reply_markup=kb)
    return MSG_CONFIRM


@admin_only
async def msg_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_create":
        await query.message.edit_text("❌ تم إلغاء العملية.")
        context.user_data.clear()
        return ConversationHandler.END

    d = context.user_data["new_msg"]
    mtype_map = {
        "none": MediaType.NONE, "photo": MediaType.PHOTO,
        "video": MediaType.VIDEO, "audio": MediaType.AUDIO,
        "document": MediaType.DOCUMENT,
    }
    dest_map = {
        "never": DestructMode.NEVER,
        "immediate": DestructMode.IMMEDIATE,
        "after_time": DestructMode.AFTER_TIME,
    }

    with get_session() as session:
        msg = db.create_broadcast_message(
            session,
            title=d.get("title"),
            text=d.get("text"),
            media_type=mtype_map.get(d.get("media_type", "none"), MediaType.NONE),
            media_file_id=d.get("media_file_id"),
            is_paid=d.get("is_paid", False),
            stars_price=d.get("stars_price", 0),
            protect_content=d.get("protect_content", True),
            destruct_mode=dest_map.get(d.get("destruct_mode", "never"), DestructMode.NEVER),
            destruct_seconds=d.get("destruct_seconds", 0),
            scheduled_at=d.get("scheduled_at"),
            interval_hours=d.get("interval_hours"),
            status=MessageStatus.DRAFT,
            created_by=update.effective_user.id,
        )
        msg_id = msg.id

    if d.get("schedule_choice") == "sched_now":
        await query.message.edit_text("⏳ <b>جارٍ البث الآن...</b>", parse_mode="HTML")
        stats = await broadcast(context.bot, msg_id)
        await query.message.reply_text(
            f"✅ <b>اكتمل البث!</b>\n\n"
            f"📤 الإجمالي: {stats.get('total', 0)}\n"
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
            run_at=d.get("scheduled_at"),
            interval_hours=d.get("interval_hours"),
        )
        await query.message.edit_text(
            f"✅ <b>تم جدولة الرسالة #{msg_id}</b>",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )

    context.user_data.clear()
    return ConversationHandler.END


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
                    pattern="^(admin_new_msg|admin_manage|admin_stats|admin_invite"
                            "|admin_settings|admin_admins|admin_ban|admin_unban|admin_back)$",
                ),
            ],
            INVITE_MENU: [
                CallbackQueryHandler(invite_cb, pattern="^(invite_my|invite_new|admin_back)$"),
            ],
            SETTINGS_MENU: [
                CallbackQueryHandler(settings_cb, pattern="^(set_welcome_text|set_welcome_toggle|admin_back)$"),
                CallbackQueryHandler(admins_cb,   pattern="^(admins_add|admins_remove|admin_back)$"),
            ],
            WELCOME_TEXT_INPUT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, welcome_text_input)],
            ADD_ADMIN_INPUT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_input)],
            REMOVE_ADMIN_INPUT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_admin_input)],
            MSG_TITLE:           [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_title)],
            MSG_MEDIA_TYPE:      [CallbackQueryHandler(msg_media_type, pattern=r"^mtype_")],
            MSG_CONTENT:         [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_content)],
            MSG_MEDIA_UPLOAD: [
                MessageHandler(
                    (filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.Document.ALL)
                    & ~filters.COMMAND,
                    msg_media_upload,
                )
            ],
            MSG_PAID_CHOICE:     [CallbackQueryHandler(msg_paid_choice,     pattern=r"^paid_")],
            MSG_STARS_AMOUNT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_stars_amount)],
            MSG_PROTECT:         [CallbackQueryHandler(msg_protect,          pattern=r"^protect_")],
            MSG_DESTRUCT_MODE:   [CallbackQueryHandler(msg_destruct_mode,    pattern=r"^dest_")],
            MSG_DESTRUCT_TIME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_destruct_time)],
            MSG_SCHEDULE_CHOICE: [CallbackQueryHandler(msg_schedule_choice,  pattern=r"^sched_")],
            MSG_SCHEDULE_TIME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_schedule_time)],
            MSG_INTERVAL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_interval)],
            MSG_CONFIRM:         [CallbackQueryHandler(msg_confirm, pattern=r"^(confirm_create|cancel_create)$")],
            BROADCAST_MANAGE: [
                CallbackQueryHandler(manage_msg_cb,  pattern=r"^mgr_\d+$"),
                CallbackQueryHandler(send_now_cb,    pattern=r"^send_now_\d+$"),
                CallbackQueryHandler(del_msg_cb,     pattern=r"^del_msg_\d+$"),
                CallbackQueryHandler(admin_menu_cb,  pattern="^admin_(manage|back)$"),
            ],
            BAN_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ban_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    app.add_handler(conv)
