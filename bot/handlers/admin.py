"""
Admin control panel — fully integrated with all features.
"""
import json
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
from bot.services.message_sender import send_configured_message

logger = logging.getLogger(__name__)

# ─── States ──────────────────────────────────────────────────────────────────
(
    MENU,
    # Broadcast wizard
    BC_CONTENT, BC_PAID, BC_STARS, BC_PROTECT, BC_DESTRUCT,
    BC_TARGET, BC_SCHEDULE, BC_SCHEDULE_TIME, BC_INTERVAL,
    BC_BUTTONS_MENU, BC_BTN_NAME, BC_BTN_URL,
    BC_CONFIRM,
    # Manage
    MANAGE_LIST, MANAGE_DETAIL,
    # Welcome
    WEL_PANEL, WEL_CONTENT, WEL_DESTRUCT, WEL_BTN_NAME, WEL_BTN_URL,
    # Start message
    SM_PANEL, SM_CONTENT, SM_DESTRUCT, SM_PRICE, SM_BTN_NAME, SM_BTN_URL,
    # Admin mgmt
    BAN_INPUT, ADD_ADMIN, REM_ADMIN,
    # Invite
    INVITE_MENU,
) = range(31)


# ─── Admin guard ──────────────────────────────────────────────────────────────

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        uid = update.effective_user.id
        if not config.is_admin(uid):
            if update.callback_query:
                await update.callback_query.answer("🚫 غير مصرح", show_alert=True)
            elif update.message:
                await update.message.reply_text("🚫 للمشرفين فقط.")
            return ConversationHandler.END
        return await func(update, context, *a, **kw)
    return wrapper


# ─── Shared keyboards ────────────────────────────────────────────────────────

def _menu_kb():
    # Check notification setting
    with get_session() as session:
        notif = db.get_setting(session, "notify_new_user", "true")
    notif_icon = "🔔" if notif == "true" else "🔕"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 إنشاء رسالة بث",    callback_data="m_new")],
        [InlineKeyboardButton("📋 إدارة الرسائل",      callback_data="m_manage")],
        [InlineKeyboardButton("📊 الإحصائيات",         callback_data="m_stats")],
        [InlineKeyboardButton("🔗 رابط دعوة",          callback_data="m_invite")],
        [InlineKeyboardButton("📩 رسالة الترحيب",      callback_data="m_welcome")],
        [InlineKeyboardButton("🚀 رسالة /Start",       callback_data="m_startmsg")],
        [InlineKeyboardButton("👑 إدارة المشرفين",     callback_data="m_admins")],
        [InlineKeyboardButton(f"{notif_icon} إشعارات الانضمام", callback_data="m_notif")],
        [InlineKeyboardButton("🚫 حظر", callback_data="m_ban"),
         InlineKeyboardButton("✅ رفع حظر", callback_data="m_unban")],
    ])

CXL = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cxl")]])


def _back(cb="go_menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=cb)]])


# ─── Entry & Menu ─────────────────────────────────────────────────────────────

@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    txt = "🛠️ <b>لوحة التحكم</b>\n\nاختر الإجراء:"
    if update.message:
        await update.message.reply_text(txt, parse_mode="HTML", reply_markup=_menu_kb())
    else:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(txt, parse_mode="HTML", reply_markup=_menu_kb())
        except Exception:
            await update.callback_query.message.reply_text(txt, parse_mode="HTML", reply_markup=_menu_kb())
    return MENU


@admin_only
async def cxl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("تم الإلغاء")
    context.user_data.clear()
    await update.callback_query.edit_message_text("❌ تم الإلغاء.", reply_markup=_menu_kb())
    return MENU


@admin_only
async def menu_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    if d == "m_new":     return await _bc_start(q, context)
    if d == "m_manage":  return await _manage_list(q)
    if d == "m_stats":   return await _stats(q)
    if d == "m_invite":  return await _invite_menu(q)
    if d == "m_welcome": return await _wel_panel(q)
    if d == "m_startmsg":return await _sm_panel(q)
    if d == "m_admins":  return await _admins_menu(q, context)
    if d == "m_notif":
        with get_session() as session:
            current = db.get_setting(session, "notify_new_user", "true")
            new_val = "false" if current == "true" else "true"
            db.set_setting(session, "notify_new_user", new_val)
        status = "🔔 مفعّلة" if new_val == "true" else "🔕 معطّلة"
        await q.answer(f"إشعارات الانضمام: {status}", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=_menu_kb())
        except Exception:
            pass
        return MENU
    if d == "m_ban":
        await q.message.reply_text("🚫 أدخل معرف المستخدم لحظره:", reply_markup=CXL)
        context.user_data["ban_action"] = "ban"
        return BAN_INPUT
    if d == "m_unban":
        await q.message.reply_text("✅ أدخل معرف المستخدم لرفع حظره:", reply_markup=CXL)
        context.user_data["ban_action"] = "unban"
        return BAN_INPUT
    if d == "go_menu":
        return await cmd_admin(update, context)
    return MENU


# ═══════════════════════════════════════════════════════════════════════════════
# BROADCAST WIZARD
# ═══════════════════════════════════════════════════════════════════════════════

async def _bc_start(q, context):
    context.user_data["bc"] = {"buttons": []}
    await q.message.reply_text(
        "📝 <b>أرسل محتوى الرسالة الآن</b>\n\n"
        "يمكنك إرسال: نص، صورة، فيديو، صوت، أو مستند.\n"
        "<i>أرسل رسالة واحدة تحتوي على كل ما تريد.</i>",
        parse_mode="HTML", reply_markup=CXL,
    )
    return BC_CONTENT


@admin_only
async def bc_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.message
    bc = context.user_data["bc"]
    bc["text"] = m.caption or m.text or ""
    if m.photo:
        bc["media_type"], bc["file_id"] = "photo", m.photo[-1].file_id
    elif m.video:
        bc["media_type"], bc["file_id"] = "video", m.video.file_id
    elif m.audio or m.voice:
        bc["media_type"], bc["file_id"] = "audio", (m.audio or m.voice).file_id
    elif m.document:
        bc["media_type"], bc["file_id"] = "document", m.document.file_id
    else:
        bc["media_type"], bc["file_id"] = "none", None

    await m.reply_text(
        "💵 <b>نوع الوصول</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🆓 مجاني", callback_data="bc_free")],
            [InlineKeyboardButton("💰 مدفوع بالنجوم", callback_data="bc_paid")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cxl")],
        ]),
    )
    return BC_PAID


@admin_only
async def bc_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    bc = context.user_data["bc"]
    if q.data == "bc_paid":
        bc["is_paid"] = True
        await q.message.reply_text("⭐ أدخل عدد النجوم:", reply_markup=CXL)
        return BC_STARS
    bc["is_paid"], bc["stars"] = False, 0
    return await _bc_ask_protect(q.message)


@admin_only
async def bc_stars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        s = int(update.message.text.strip()); assert s >= 1
    except:
        await update.message.reply_text("❌ أدخل رقماً > 0", reply_markup=CXL)
        return BC_STARS
    context.user_data["bc"]["stars"] = s
    return await _bc_ask_protect(update.message)


async def _bc_ask_protect(msg):
    await msg.reply_text("🔐 <b>حماية المحتوى</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 نعم — منع التوجيه", callback_data="bc_prot_yes")],
        [InlineKeyboardButton("🔓 لا", callback_data="bc_prot_no")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cxl")],
    ]))
    return BC_PROTECT


@admin_only
async def bc_protect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data["bc"]["protect"] = (q.data == "bc_prot_yes")
    await q.message.reply_text(
        "💣 <b>ذاتية التدمير</b>\n\n• <b>0</b> = لا تُحذف\n• أدخل عدد الثواني",
        parse_mode="HTML", reply_markup=CXL,
    )
    return BC_DESTRUCT


@admin_only
async def bc_destruct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        s = int(update.message.text.strip()); assert s >= 0
    except:
        await update.message.reply_text("❌ أدخل رقماً ≥ 0", reply_markup=CXL)
        return BC_DESTRUCT
    context.user_data["bc"]["destruct"] = s
    return await _bc_ask_target(update.message)


async def _bc_ask_target(msg):
    with get_session() as session:
        uc = db.get_user_count(session)
        gc = db.get_group_count(session)
    await msg.reply_text(
        f"📡 <b>هدف البث</b>\n👥 {uc} مشترك | 💬 {gc} مجموعة",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"👥 المشتركون ({uc})", callback_data="tgt_users")],
            [InlineKeyboardButton(f"💬 المجموعات ({gc})", callback_data="tgt_groups")],
            [InlineKeyboardButton(f"🌐 الكل ({uc+gc})", callback_data="tgt_all")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cxl")],
        ]),
    )
    return BC_TARGET


@admin_only
async def bc_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tmap = {"tgt_users": "users", "tgt_groups": "groups", "tgt_all": "all"}
    context.user_data["bc"]["target"] = tmap.get(q.data, "users")
    await q.message.reply_text("⏰ <b>جدولة الإرسال</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 فوري", callback_data="sch_now")],
        [InlineKeyboardButton("📅 وقت محدد", callback_data="sch_time")],
        [InlineKeyboardButton("🔁 متكرر", callback_data="sch_repeat")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cxl")],
    ]))
    return BC_SCHEDULE


@admin_only
async def bc_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    bc = context.user_data["bc"]
    if q.data == "sch_now":
        bc["sched_at"], bc["interval"] = None, None
        return await _bc_ask_buttons(q.message)
    if q.data == "sch_time":
        await q.message.reply_text("📅 أدخل: <code>YYYY-MM-DD HH:MM</code>", parse_mode="HTML", reply_markup=CXL)
        return BC_SCHEDULE_TIME
    if q.data == "sch_repeat":
        await q.message.reply_text("🔁 أدخل عدد ساعات التكرار:", reply_markup=CXL)
        return BC_INTERVAL


@admin_only
async def bc_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        dt = datetime.datetime.strptime(update.message.text.strip(), "%Y-%m-%d %H:%M")
    except:
        await update.message.reply_text("❌ صيغة خاطئة", reply_markup=CXL)
        return BC_SCHEDULE_TIME
    context.user_data["bc"]["sched_at"] = dt
    context.user_data["bc"]["interval"] = None
    return await _bc_ask_buttons(update.message)


@admin_only
async def bc_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        h = float(update.message.text.strip()); assert h > 0
    except:
        await update.message.reply_text("❌ أدخل رقماً > 0", reply_markup=CXL)
        return BC_INTERVAL
    context.user_data["bc"]["interval"] = h
    context.user_data["bc"]["sched_at"] = None
    return await _bc_ask_buttons(update.message)


async def _bc_ask_buttons(msg):
    await msg.reply_text("🔘 <b>هل تريد إضافة أزرار للرسالة؟</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ إضافة زر", callback_data="btn_add")],
        [InlineKeyboardButton("⏩ تخطي (بدون أزرار)", callback_data="btn_skip")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cxl")],
    ]))
    return BC_BUTTONS_MENU


@admin_only
async def bc_buttons_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "btn_add":
        await q.message.reply_text("📝 أرسل اسم الزر:", reply_markup=CXL)
        return BC_BTN_NAME
    return await _bc_confirm(q.message, context)


@admin_only
async def bc_btn_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bc"]["_btn_name"] = update.message.text.strip()
    await update.message.reply_text("🔗 أرسل رابط الزر:", reply_markup=CXL)
    return BC_BTN_URL


@admin_only
async def bc_btn_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ يجب أن يبدأ بـ http", reply_markup=CXL)
        return BC_BTN_URL
    bc = context.user_data["bc"]
    name = bc.pop("_btn_name", "زر")
    bc["buttons"].append({"text": name, "url": url})
    await update.message.reply_text(
        f"✅ تم إضافة: {name}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ زر آخر", callback_data="btn_add")],
            [InlineKeyboardButton("✅ انتهيت", callback_data="btn_skip")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cxl")],
        ]),
    )
    return BC_BUTTONS_MENU


async def _bc_confirm(msg, context):
    bc = context.user_data["bc"]
    paid = f"💰 {bc.get('stars', 0)} ⭐" if bc.get("is_paid") else "🆓 مجاني"
    dest = "🔴 لا" if bc.get("destruct", 0) == 0 else f"⏱️ {bc['destruct']}ث"
    tgt = {"users": "👥 المشتركون", "groups": "💬 المجموعات", "all": "🌐 الكل"}.get(bc.get("target", "users"))
    sch = "🚀 فوري" if not bc.get("interval") and not bc.get("sched_at") else (
        f"🔁 كل {bc['interval']}h" if bc.get("interval") else str(bc.get("sched_at")))
    btns = len(bc.get("buttons", []))

    await msg.reply_text(
        f"📋 <b>مراجعة الرسالة</b>\n\n"
        f"🎬 النوع: {bc.get('media_type', 'none')}\n"
        f"💵 {paid}\n🔒 حماية: {'✅' if bc.get('protect') else '❌'}\n"
        f"💣 تدمير: {dest}\n📡 {tgt}\n⏰ {sch}\n🔘 أزرار: {btns}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأكيد وإرسال", callback_data="bc_go")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cxl")],
        ]),
    )
    return BC_CONFIRM


@admin_only
async def bc_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    bc = context.user_data["bc"]
    mtype_map = {"none": MediaType.NONE, "photo": MediaType.PHOTO, "video": MediaType.VIDEO,
                 "audio": MediaType.AUDIO, "document": MediaType.DOCUMENT}
    dest_mode = DestructMode.AFTER_TIME if bc.get("destruct", 0) > 0 else DestructMode.NEVER

    with get_session() as session:
        msg_obj = db.create_broadcast_message(
            session,
            title=None, text=bc.get("text"),
            media_type=mtype_map.get(bc.get("media_type", "none"), MediaType.NONE),
            media_file_id=bc.get("file_id"),
            is_paid=bc.get("is_paid", False), stars_price=bc.get("stars", 0),
            protect_content=bc.get("protect", True),
            destruct_mode=dest_mode, destruct_seconds=bc.get("destruct", 0),
            scheduled_at=bc.get("sched_at"), interval_hours=bc.get("interval"),
            status=MessageStatus.DRAFT, created_by=update.effective_user.id,
            buttons_json=json.dumps(bc.get("buttons", []), ensure_ascii=False),
        )
        msg_id = msg_obj.id

    target = bc.get("target", "users")
    is_now = not bc.get("interval") and not bc.get("sched_at")

    if is_now:
        await q.message.edit_text("⏳ <b>جارٍ البث...</b>", parse_mode="HTML")
        stats = await broadcast(context.bot, msg_id, target=target)
        await q.message.reply_text(
            f"✅ <b>اكتمل!</b>\n👥 {stats.get('users_total',0)} | 💬 {stats.get('groups_total',0)}\n"
            f"✅ {stats.get('success',0)} | ❌ {stats.get('failed',0)}",
            parse_mode="HTML", reply_markup=_menu_kb(),
        )
    else:
        with get_session() as session:
            db.update_message_status(session, msg_id, MessageStatus.SCHEDULED)
        sched.schedule_broadcast(context.bot, msg_id,
                                 run_at=bc.get("sched_at"), interval_hours=bc.get("interval"), target=target)
        await q.message.edit_text(f"✅ جُدولت الرسالة #{msg_id}", reply_markup=_menu_kb())

    context.user_data.clear()
    return MENU


# ═══════════════════════════════════════════════════════════════════════════════
# MANAGE MESSAGES
# ═══════════════════════════════════════════════════════════════════════════════

async def _manage_list(q):
    with get_session() as session:
        raw = db.get_all_messages(session)
        msgs = [{"id": m.id, "status": m.status.value, "is_paid": m.is_paid,
                 "media": m.media_type.value, "created": m.created_at.strftime("%m-%d %H:%M")} for m in raw[:10]]
    if not msgs:
        await q.message.reply_text("📭 لا توجد رسائل.", reply_markup=_back())
        return MENU
    icons = {"draft": "📝", "scheduled": "⏰", "sent": "✅", "cancelled": "❌"}
    rows = [[InlineKeyboardButton(
        f"{icons.get(m['status'],'❓')}{'💰' if m['is_paid'] else '🆓'} #{m['id']} {m['created']}",
        callback_data=f"mgr_{m['id']}")] for m in msgs]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="go_menu")])
    await q.message.reply_text("📋 <b>الرسائل:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    return MANAGE_LIST


@admin_only
async def manage_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "go_menu": return await cmd_admin(update, context)
    mid = int(q.data.split("_")[1])
    with get_session() as session:
        m = db.get_message_by_id(session, mid)
        if not m:
            await q.message.reply_text("❌ غير موجودة", reply_markup=_back())
            return MENU
        d = {"id": m.id, "status": m.status.value, "paid": m.is_paid,
             "stars": m.stars_price, "media": m.media_type.value,
             "protect": m.protect_content, "dest": m.destruct_seconds,
             "created": m.created_at.strftime("%Y-%m-%d %H:%M")}
    paid = f"💰 {d['stars']}⭐" if d["paid"] else "🆓"
    await q.message.edit_text(
        f"📨 <b>#{d['id']}</b>\n{d['media']} | {paid} | 🔒{'✅' if d['protect'] else '❌'}\n💣 {d['dest']}ث | {d['status']} | {d['created']}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 إرسال", callback_data=f"send_{mid}"),
             InlineKeyboardButton("🗑️ حذف", callback_data=f"del_{mid}")],
            [InlineKeyboardButton("🔙 القائمة", callback_data="go_manage")],
        ]),
    )
    return MANAGE_DETAIL


@admin_only
async def manage_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "go_manage": return await _manage_list(q)
    if q.data == "go_menu": return await cmd_admin(update, context)
    action, mid = q.data.split("_")[0], int(q.data.split("_")[1])
    if action == "send":
        await q.message.edit_text("⏳ جارٍ البث...")
        stats = await broadcast(context.bot, mid, target="users")
        await q.message.reply_text(f"✅ نجح: {stats.get('success',0)} | فشل: {stats.get('failed',0)}", reply_markup=_back("go_manage"))
    elif action == "del":
        sched.cancel_broadcast(mid)
        with get_session() as session:
            db.delete_message_record(session, mid)
        await q.message.edit_text(f"🗑️ حُذفت #{mid}", reply_markup=_back("go_manage"))
    return MANAGE_DETAIL


# ═══════════════════════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════════════════════

async def _stats(q):
    with get_session() as session:
        uc = db.get_user_count(session)
        gc = db.get_group_count(session)
        stars = db.get_total_stars_earned(session)
        msgs = db.get_all_messages(session)
        sent = sum(1 for m in msgs if m.status == MessageStatus.SENT)
        schd = sum(1 for m in msgs if m.status == MessageStatus.SCHEDULED)
    await q.message.reply_text(
        f"📊 <b>الإحصائيات</b>\n\n👥 مشتركون: {uc}\n💬 مجموعات: {gc}\n"
        f"⭐ نجوم: {stars}\n📨 مُرسلة: {sent}\n⏰ مجدولة: {schd}",
        parse_mode="HTML", reply_markup=_back(),
    )
    return MENU


# ═══════════════════════════════════════════════════════════════════════════════
# INVITE
# ═══════════════════════════════════════════════════════════════════════════════

async def _invite_menu(q):
    await q.message.reply_text("🔗 <b>روابط الدعوة</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 رابطي", callback_data="inv_my")],
        [InlineKeyboardButton("✨ رابط جديد", callback_data="inv_new")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="go_menu")],
    ]))
    return INVITE_MENU


@admin_only
async def invite_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    from bot.services.referral import get_or_create_user_invite, build_invite_url
    if q.data == "inv_my":
        token = get_or_create_user_invite(update.effective_user.id)
        await q.message.reply_text(f"🔗 <code>{build_invite_url(token)}</code>", parse_mode="HTML")
    elif q.data == "inv_new":
        with get_session() as session:
            link = db.create_invite_link(session, update.effective_user.id)
            token = link.token
        await q.message.reply_text(f"✨ <code>{build_invite_url(token)}</code>", parse_mode="HTML")
    elif q.data == "go_menu":
        return await cmd_admin(update, context)
    return INVITE_MENU


# ═══════════════════════════════════════════════════════════════════════════════
# WELCOME MESSAGE PANEL
# ═══════════════════════════════════════════════════════════════════════════════

def _wel_kb(cfg):
    en = "🟢 مفعّلة" if cfg["enabled"] else "🔴 معطّلة"
    pr = "🔒" if cfg["protect_content"] else "🔓"
    ds = f"{cfg['destruct_seconds']}ث" if cfg["destruct_seconds"] > 0 else "لا"
    bn = len(cfg["buttons"])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"الحالة: {en}", callback_data="w_toggle")],
        [InlineKeyboardButton("✏️ تعديل المحتوى", callback_data="w_content")],
        [InlineKeyboardButton(f"🔘 الأزرار ({bn})", callback_data="w_btns")],
        [InlineKeyboardButton(f"الحماية: {pr}", callback_data="w_prot")],
        [InlineKeyboardButton(f"💣 التدمير: {ds}", callback_data="w_dest")],
        [InlineKeyboardButton("👁️ معاينة", callback_data="w_preview")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="go_menu")],
    ])


async def _wel_panel(q):
    with get_session() as session:
        cfg = db.get_welcome_config(session)
    await q.message.reply_text("📩 <b>إعدادات رسالة الترحيب</b>", parse_mode="HTML", reply_markup=_wel_kb(cfg))
    return WEL_PANEL


@admin_only
async def wel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    d = q.data
    with get_session() as session:
        cfg = db.get_welcome_config(session)

        if d == "w_toggle":
            db.save_welcome_config(session, enabled=not cfg["enabled"])
            cfg["enabled"] = not cfg["enabled"]
            await q.message.edit_reply_markup(reply_markup=_wel_kb(cfg))
        elif d == "w_prot":
            db.save_welcome_config(session, protect_content=not cfg["protect_content"])
            cfg["protect_content"] = not cfg["protect_content"]
            await q.message.edit_reply_markup(reply_markup=_wel_kb(cfg))
        elif d == "w_content":
            await q.message.reply_text("📝 أرسل المحتوى الجديد (نص أو وسائط):", reply_markup=CXL)
            return WEL_CONTENT
        elif d == "w_dest":
            await q.message.reply_text("💣 أدخل ثواني التدمير (0 = لا):", reply_markup=CXL)
            return WEL_DESTRUCT
        elif d == "w_preview":
            text = (cfg.get("text") or "معاينة").replace("{name}", "أحمد").replace("{id}", "123")
            await send_configured_message(
                context.bot, update.effective_user.id, text=text,
                media_type=cfg["media_type"], media_file_id=cfg["media_file_id"],
                protect_content=cfg["protect_content"], buttons=cfg["buttons"], destruct_seconds=0,
            )
        elif d == "w_btns":
            return await _wel_btns_panel(q, cfg)
        elif d == "go_menu":
            return await cmd_admin(update, context)
    return WEL_PANEL


async def _wel_btns_panel(q, cfg):
    rows = [[InlineKeyboardButton(f"❌ {b['text']}", callback_data=f"wbd_{i}")] for i, b in enumerate(cfg["buttons"])]
    rows.append([InlineKeyboardButton("➕ إضافة زر", callback_data="wba")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="m_welcome")])
    await q.message.reply_text("🔘 <b>الأزرار الحالية:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    return WEL_PANEL


@admin_only
async def wel_btn_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    d = q.data
    if d == "wba":
        await q.message.reply_text("📝 أرسل اسم الزر:", reply_markup=CXL)
        return WEL_BTN_NAME
    if d.startswith("wbd_"):
        idx = int(d.split("_")[1])
        with get_session() as session:
            cfg = db.get_welcome_config(session)
            btns = cfg["buttons"]
            if 0 <= idx < len(btns):
                btns.pop(idx)
                db.save_welcome_config(session, buttons=btns)
            cfg["buttons"] = btns
        return await _wel_btns_panel(q, cfg)
    return WEL_PANEL


@admin_only
async def wel_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.message
    text = m.caption or m.text or ""
    mt, fid = "none", None
    if m.photo: mt, fid = "photo", m.photo[-1].file_id
    elif m.video: mt, fid = "video", m.video.file_id
    elif m.audio: mt, fid = "audio", m.audio.file_id
    elif m.document: mt, fid = "document", m.document.file_id
    with get_session() as session:
        db.save_welcome_config(session, text=text, media_type=mt, media_file_id=fid)
    await m.reply_text("✅ تم التحديث.", reply_markup=_back("m_welcome"))
    return MENU


@admin_only
async def wel_destruct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: s = int(update.message.text.strip()); assert s >= 0
    except:
        await update.message.reply_text("❌ رقم ≥ 0", reply_markup=CXL); return WEL_DESTRUCT
    with get_session() as session:
        db.save_welcome_config(session, destruct_seconds=s)
    await update.message.reply_text("✅ تم.", reply_markup=_back("m_welcome"))
    return MENU


@admin_only
async def wel_btn_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["_btn"] = update.message.text.strip()
    await update.message.reply_text("🔗 أرسل رابط الزر:", reply_markup=CXL)
    return WEL_BTN_URL


@admin_only
async def wel_btn_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ يجب http", reply_markup=CXL); return WEL_BTN_URL
    name = context.user_data.pop("_btn", "زر")
    with get_session() as session:
        cfg = db.get_welcome_config(session)
        btns = cfg["buttons"]
        btns.append({"text": name, "url": url})
        db.save_welcome_config(session, buttons=btns)
    await update.message.reply_text(f"✅ أُضيف: {name}", reply_markup=_back("m_welcome"))
    return MENU


# ═══════════════════════════════════════════════════════════════════════════════
# START MESSAGE PANEL
# ═══════════════════════════════════════════════════════════════════════════════

def _sm_kb(cfg):
    en = "🟢 مفعّلة" if cfg["enabled"] else "🔴 معطّلة"
    paid = f"💰 {cfg['stars_price']}⭐" if cfg["is_paid"] else "🆓 مجاني"
    pr = "🔒" if cfg["protect_content"] else "🔓"
    ds = f"{cfg['destruct_seconds']}ث" if cfg["destruct_seconds"] > 0 else "لا"
    mode = "🔁 دائماً" if cfg["show_mode"] == "always" else "1️⃣ مرة واحدة"
    bn = len(cfg["buttons"])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"الحالة: {en}", callback_data="s_toggle")],
        [InlineKeyboardButton("✏️ تعديل المحتوى", callback_data="s_content")],
        [InlineKeyboardButton(f"النوع: {paid}", callback_data="s_paid")],
        [InlineKeyboardButton(f"🔘 الأزرار ({bn})", callback_data="s_btns")],
        [InlineKeyboardButton(f"الظهور: {mode}", callback_data="s_mode")],
        [InlineKeyboardButton(f"الحماية: {pr}", callback_data="s_prot")],
        [InlineKeyboardButton(f"💣 التدمير: {ds}", callback_data="s_dest")],
        [InlineKeyboardButton("👁️ معاينة", callback_data="s_preview")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="go_menu")],
    ])


async def _sm_panel(q):
    with get_session() as session:
        cfg = db.get_start_msg_config(session)
    await q.message.reply_text("🚀 <b>إعدادات رسالة /Start</b>", parse_mode="HTML", reply_markup=_sm_kb(cfg))
    return SM_PANEL


@admin_only
async def sm_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    d = q.data
    with get_session() as session:
        cfg = db.get_start_msg_config(session)

        if d == "s_toggle":
            db.save_start_msg_config(session, enabled=not cfg["enabled"])
            cfg["enabled"] = not cfg["enabled"]
            await q.message.edit_reply_markup(reply_markup=_sm_kb(cfg))
        elif d == "s_prot":
            db.save_start_msg_config(session, protect_content=not cfg["protect_content"])
            cfg["protect_content"] = not cfg["protect_content"]
            await q.message.edit_reply_markup(reply_markup=_sm_kb(cfg))
        elif d == "s_mode":
            new = "once_after_join" if cfg["show_mode"] == "always" else "always"
            db.save_start_msg_config(session, show_mode=new)
            cfg["show_mode"] = new
            await q.message.edit_reply_markup(reply_markup=_sm_kb(cfg))
        elif d == "s_paid":
            if cfg["is_paid"]:
                db.save_start_msg_config(session, is_paid=False)
                cfg["is_paid"] = False
                await q.message.edit_reply_markup(reply_markup=_sm_kb(cfg))
            else:
                await q.message.reply_text("⭐ أرسل السعر بالنجوم:", reply_markup=CXL)
                return SM_PRICE
        elif d == "s_content":
            await q.message.reply_text("📝 أرسل المحتوى الجديد:", reply_markup=CXL)
            return SM_CONTENT
        elif d == "s_dest":
            await q.message.reply_text("💣 أدخل ثواني التدمير (0 = لا):", reply_markup=CXL)
            return SM_DESTRUCT
        elif d == "s_preview":
            text = (cfg.get("text") or "معاينة").replace("{name}", "أحمد").replace("{id}", "123")
            await send_configured_message(
                context.bot, update.effective_user.id, text=text,
                media_type=cfg["media_type"], media_file_id=cfg["media_file_id"],
                protect_content=cfg["protect_content"], buttons=cfg["buttons"], destruct_seconds=0,
            )
        elif d == "s_btns":
            return await _sm_btns_panel(q, cfg)
        elif d == "go_menu":
            return await cmd_admin(update, context)
    return SM_PANEL


async def _sm_btns_panel(q, cfg):
    rows = [[InlineKeyboardButton(f"❌ {b['text']}", callback_data=f"sbd_{i}")] for i, b in enumerate(cfg["buttons"])]
    rows.append([InlineKeyboardButton("➕ إضافة زر", callback_data="sba")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="m_startmsg")])
    await q.message.reply_text("🔘 <b>أزرار رسالة /Start:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    return SM_PANEL


@admin_only
async def sm_btn_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "sba":
        await q.message.reply_text("📝 أرسل اسم الزر:", reply_markup=CXL)
        return SM_BTN_NAME
    if q.data.startswith("sbd_"):
        idx = int(q.data.split("_")[1])
        with get_session() as session:
            cfg = db.get_start_msg_config(session)
            btns = cfg["buttons"]
            if 0 <= idx < len(btns):
                btns.pop(idx)
                db.save_start_msg_config(session, buttons=btns)
            cfg["buttons"] = btns
        return await _sm_btns_panel(q, cfg)
    return SM_PANEL


@admin_only
async def sm_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.message
    text = m.caption or m.text or ""
    mt, fid = "none", None
    if m.photo: mt, fid = "photo", m.photo[-1].file_id
    elif m.video: mt, fid = "video", m.video.file_id
    elif m.audio: mt, fid = "audio", m.audio.file_id
    elif m.document: mt, fid = "document", m.document.file_id
    with get_session() as session:
        db.save_start_msg_config(session, text=text, media_type=mt, media_file_id=fid)
    await m.reply_text("✅ تم.", reply_markup=_back("m_startmsg"))
    return MENU


@admin_only
async def sm_destruct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: s = int(update.message.text.strip()); assert s >= 0
    except:
        await update.message.reply_text("❌ رقم ≥ 0", reply_markup=CXL); return SM_DESTRUCT
    with get_session() as session:
        db.save_start_msg_config(session, destruct_seconds=s)
    await update.message.reply_text("✅ تم.", reply_markup=_back("m_startmsg"))
    return MENU


@admin_only
async def sm_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: p = int(update.message.text.strip()); assert p >= 1
    except:
        await update.message.reply_text("❌ رقم > 0", reply_markup=CXL); return SM_PRICE
    with get_session() as session:
        db.save_start_msg_config(session, is_paid=True, stars_price=p)
    await update.message.reply_text("✅ تم.", reply_markup=_back("m_startmsg"))
    return MENU


@admin_only
async def sm_btn_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["_btn"] = update.message.text.strip()
    await update.message.reply_text("🔗 أرسل رابط الزر:", reply_markup=CXL)
    return SM_BTN_URL


@admin_only
async def sm_btn_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ يجب http", reply_markup=CXL); return SM_BTN_URL
    name = context.user_data.pop("_btn", "زر")
    with get_session() as session:
        cfg = db.get_start_msg_config(session)
        btns = cfg["buttons"]
        btns.append({"text": name, "url": url})
        db.save_start_msg_config(session, buttons=btns)
    await update.message.reply_text(f"✅ أُضيف: {name}", reply_markup=_back("m_startmsg"))
    return MENU


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN MANAGEMENT & BAN
# ═══════════════════════════════════════════════════════════════════════════════

async def _admins_menu(q, context):
    with get_session() as session:
        dyn = db.list_admins(session)
        dlist = [(a.telegram_id, a.note or "") for a in dyn]
    lines = "\n".join(f"• <code>{uid}</code> {n}" for uid, n in dlist) or "لا يوجد"
    static = "\n".join(f"• <code>{uid}</code>" for uid in config._STATIC_ADMIN_IDS) or "—"
    await q.message.reply_text(
        f"👑 <b>المشرفون</b>\n\n📌 ثابتون:\n{static}\n\n➕ ديناميكيون:\n{lines}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ إضافة", callback_data="adm_add")],
            [InlineKeyboardButton("➖ إزالة", callback_data="adm_rem")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="go_menu")],
        ]),
    )
    return MENU


@admin_only
async def adm_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "adm_add":
        await q.message.reply_text("➕ أرسل معرف المشرف الجديد:", reply_markup=CXL)
        return ADD_ADMIN
    if q.data == "adm_rem":
        await q.message.reply_text("➖ أرسل معرف المشرف للإزالة:", reply_markup=CXL)
        return REM_ADMIN
    return await cmd_admin(update, context)


@admin_only
async def do_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split(maxsplit=1)
    try: tid = int(parts[0])
    except:
        await update.message.reply_text("❌ معرف غير صالح", reply_markup=CXL); return ADD_ADMIN
    note = parts[1] if len(parts) > 1 else ""
    with get_session() as session:
        ok = db.add_admin(session, tid, update.effective_user.id, note)
    txt = f"✅ أُضيف <code>{tid}</code>" if ok else f"⚠️ <code>{tid}</code> موجود"
    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=_menu_kb())
    if ok:
        try: await context.bot.send_message(tid, "👑 تمت ترقيتك مشرفاً! /admin")
        except: pass
    return MENU


@admin_only
async def do_rem_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: tid = int(update.message.text.strip())
    except:
        await update.message.reply_text("❌ معرف غير صالح", reply_markup=CXL); return REM_ADMIN
    if tid in config._STATIC_ADMIN_IDS:
        await update.message.reply_text("⚠️ لا يمكن إزالة مشرف ثابت.", reply_markup=_menu_kb())
        return MENU
    with get_session() as session:
        ok = db.remove_admin(session, tid)
    txt = f"✅ أُزيل <code>{tid}</code>" if ok else "❌ غير موجود"
    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=_menu_kb())
    return MENU


@admin_only
async def do_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: tid = int(update.message.text.strip())
    except:
        await update.message.reply_text("❌ أدخل رقماً", reply_markup=CXL); return BAN_INPUT
    action = context.user_data.get("ban_action", "ban")
    with get_session() as session:
        if action == "ban":
            ok = db.ban_user(session, tid)
            txt = f"✅ حُظر <code>{tid}</code>" if ok else "❌ غير موجود"
        else:
            ok = db.unban_user(session, tid)
            txt = f"✅ رُفع الحظر عن <code>{tid}</code>" if ok else "❌ غير موجود"
    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=_menu_kb())
    return MENU


@admin_only
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ تم الإلغاء.", reply_markup=_menu_kb())
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# REGISTER
# ═══════════════════════════════════════════════════════════════════════════════

_cxl = [CallbackQueryHandler(cxl_handler, pattern="^cxl$")]
_any_media = filters.TEXT | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL

def register_admin_handlers(app):
    conv = ConversationHandler(
        entry_points=[CommandHandler("admin", cmd_admin)],
        states={
            MENU: [
                CallbackQueryHandler(menu_dispatch, pattern="^m_"),
                CallbackQueryHandler(menu_dispatch, pattern="^go_menu$"),
                CallbackQueryHandler(adm_actions, pattern="^adm_"),
                CallbackQueryHandler(wel_action, pattern="^w_"),
                CallbackQueryHandler(wel_btn_actions, pattern="^wb[ad]"),
                CallbackQueryHandler(sm_action, pattern="^s_"),
                CallbackQueryHandler(sm_btn_actions, pattern="^sb[ad]"),
            ] + _cxl,
            # Broadcast wizard
            BC_CONTENT:       [MessageHandler(_any_media & ~filters.COMMAND, bc_content)] + _cxl,
            BC_PAID:          [CallbackQueryHandler(bc_paid, pattern="^bc_")] + _cxl,
            BC_STARS:         [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_stars)] + _cxl,
            BC_PROTECT:       [CallbackQueryHandler(bc_protect, pattern="^bc_prot")] + _cxl,
            BC_DESTRUCT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_destruct)] + _cxl,
            BC_TARGET:        [CallbackQueryHandler(bc_target, pattern="^tgt_")] + _cxl,
            BC_SCHEDULE:      [CallbackQueryHandler(bc_schedule, pattern="^sch_")] + _cxl,
            BC_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_schedule_time)] + _cxl,
            BC_INTERVAL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_interval)] + _cxl,
            BC_BUTTONS_MENU:  [CallbackQueryHandler(bc_buttons_menu, pattern="^btn_")] + _cxl,
            BC_BTN_NAME:      [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_btn_name)] + _cxl,
            BC_BTN_URL:       [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_btn_url)] + _cxl,
            BC_CONFIRM:       [CallbackQueryHandler(bc_confirm, pattern="^bc_go$")] + _cxl,
            # Manage
            MANAGE_LIST:   [CallbackQueryHandler(manage_detail, pattern="^(mgr_|go_)")] + _cxl,
            MANAGE_DETAIL: [CallbackQueryHandler(manage_action, pattern="^(send_|del_|go_)")] + _cxl,
            # Welcome
            WEL_PANEL:     [
                CallbackQueryHandler(wel_action, pattern="^w_"),
                CallbackQueryHandler(wel_btn_actions, pattern="^wb[ad]"),
                CallbackQueryHandler(menu_dispatch, pattern="^(go_menu|m_welcome)$"),
            ] + _cxl,
            WEL_CONTENT:   [MessageHandler(_any_media & ~filters.COMMAND, wel_content)] + _cxl,
            WEL_DESTRUCT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, wel_destruct)] + _cxl,
            WEL_BTN_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, wel_btn_name)] + _cxl,
            WEL_BTN_URL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, wel_btn_url)] + _cxl,
            # Start message
            SM_PANEL: [
                CallbackQueryHandler(sm_action, pattern="^s_"),
                CallbackQueryHandler(sm_btn_actions, pattern="^sb[ad]"),
                CallbackQueryHandler(menu_dispatch, pattern="^(go_menu|m_startmsg)$"),
            ] + _cxl,
            SM_CONTENT:    [MessageHandler(_any_media & ~filters.COMMAND, sm_content)] + _cxl,
            SM_DESTRUCT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, sm_destruct)] + _cxl,
            SM_PRICE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, sm_price)] + _cxl,
            SM_BTN_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, sm_btn_name)] + _cxl,
            SM_BTN_URL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, sm_btn_url)] + _cxl,
            # Admin
            BAN_INPUT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, do_ban)] + _cxl,
            ADD_ADMIN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, do_add_admin)] + _cxl,
            REM_ADMIN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, do_rem_admin)] + _cxl,
            # Invite
            INVITE_MENU:   [CallbackQueryHandler(invite_cb, pattern="^(inv_|go_menu)")] + _cxl,
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CommandHandler("admin", cmd_admin),
            CallbackQueryHandler(cxl_handler, pattern="^cxl$"),
        ],
        per_user=True, per_chat=True, allow_reentry=True,
    )
    app.add_handler(conv)
