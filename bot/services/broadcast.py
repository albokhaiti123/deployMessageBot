"""
Broadcast service — uses the unified message_sender.

Phase-1 fixes:
  - _send_single: NO content leak before payment.
    Paid messages → locked teaser + pay button ONLY (no text/media/buttons exposed).
  - send_paid_content_to_user: fixed return check bool(result) not `result is not None`
  - broadcast: uses mark_broadcast_executed (keeps repeating jobs SCHEDULED)
  - broadcast: uses purchase_type="broadcast" for user_has_purchased
  - _serialize_msg: includes title + target
  - delete_expired_messages: closes DB session before network calls
"""
import asyncio
import html
import json
import logging
import datetime
from typing import Optional, Literal

from telegram import Bot, InlineKeyboardButton
from telegram.error import TelegramError

from database.models import (
    get_session, BroadcastMessage, MediaType, DestructMode, MessageStatus
)
from database import db
from bot.services.message_sender import send_configured_message

logger = logging.getLogger(__name__)

BroadcastTarget = Literal["users", "groups", "all"]


def _pay_kb(message_id: int, stars: int) -> list[list[InlineKeyboardButton]]:
    """Returns extra_buttons rows for a pay button."""
    return [[
        InlineKeyboardButton(
            f"🔓 فتح المحتوى — {stars} ⭐",
            callback_data=f"pay_{message_id}"
        )
    ]]


def _build_locked_teaser(title: Optional[str], stars: int, media_count: int = 0) -> str:
    """
    FIX: safe paywall teaser that reveals ZERO paid content before purchase.
    Uses html.escape() on title.
    """
    safe_title = html.escape(title) if title else "محتوى حصري مدفوع"
    media_hint = f"\n📎 يحتوي على {media_count} وسائط مرفقة." if media_count > 0 else ""
    return (
        f"🔒 <b>{safe_title}</b>{media_hint}\n\n"
        f"هذا المحتوى مدفوع ومغلق.\n"
        f"⭐ السعر: <b>{stars} نجمة</b>\n\n"
        f"اضغط على الزر أدناه للدفع وفتح المحتوى."
    )


async def _send_single(
    bot: Bot,
    chat_id: int,
    msg_data: dict,
    user_db_id: Optional[int],
    already_paid: bool = False,
) -> list[int]:
    """Send one message to one chat using the unified sender."""
    is_locked_paid = (
        msg_data["is_paid"]
        and msg_data["stars_price"] > 0
        and not already_paid
        and user_db_id is not None
    )

    if is_locked_paid:
        # FIX: send ONLY locked teaser — no content/media/buttons leaked before payment
        teaser = _build_locked_teaser(
            msg_data.get("title"),
            msg_data["stars_price"],
            len(msg_data.get("media_list", [])),
        )
        return await send_configured_message(
            bot,
            chat_id,
            text=teaser,
            media_type="none",
            media_file_id=None,
            media_list=[],
            protect_content=msg_data["protect_content"],
            has_spoiler=False,
            buttons=[],
            extra_buttons=_pay_kb(msg_data["id"], msg_data["stars_price"]),
            destruct_seconds=0,
        )

    # Free or already-paid: deliver full content
    return await send_configured_message(
        bot,
        chat_id,
        text=msg_data["text"],
        media_type=msg_data["media_type"],
        media_file_id=msg_data["media_file_id"],
        media_list=msg_data.get("media_list", []),
        protect_content=msg_data["protect_content"],
        has_spoiler=False,
        buttons=msg_data.get("buttons", []),
        extra_buttons=None,
        destruct_seconds=msg_data["destruct_seconds"],
    )


def _serialize_msg(msg: BroadcastMessage) -> dict:
    """Serialize ORM object to a plain dict inside a session."""
    media_list = json.loads(msg.media_json or "[]") if hasattr(msg, "media_json") and msg.media_json else []
    if not media_list and msg.media_file_id and msg.media_type and msg.media_type != MediaType.NONE:
        media_list = [{"type": msg.media_type.value, "file_id": msg.media_file_id}]
    return {
        "id":               msg.id,
        "title":            msg.title,
        "text":             msg.text,
        "media_type":       msg.media_type.value if msg.media_type else "none",
        "media_file_id":    msg.media_file_id,
        "media_list":       media_list,
        "is_paid":          msg.is_paid,
        "stars_price":      msg.stars_price or 0,
        "protect_content":  msg.protect_content,
        "destruct_mode":    msg.destruct_mode.value if msg.destruct_mode else "never",
        "destruct_seconds": msg.destruct_seconds or 0,
        "interval_hours":   msg.interval_hours,
        "target":           getattr(msg, "target", "users") or "users",
        "buttons":          json.loads(msg.buttons_json or "[]"),
    }


# ─── Main broadcast ───────────────────────────────────────────────────────────

async def broadcast(
    bot: Bot,
    message_id: int,
    target: Optional[BroadcastTarget] = None,
) -> dict:
    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            return {"error": "Message not found"}
        msg_data   = _serialize_msg(msg)
        eff_target = target or msg_data["target"]
        users_data  = []
        groups_data = []
        if eff_target in ("users", "all"):
            users = db.get_all_active_users(session)
            users_data = [(u.telegram_id, u.id) for u in users]
        if eff_target in ("groups", "all"):
            groups_data = db.get_active_groups(session)

    total   = len(users_data) + len(groups_data)
    success = failed = 0

    # ── Users ────────────────────────────────────────────────────────────
    for tg_id, db_id in users_data:
        paid_ok = False
        if msg_data["is_paid"]:
            with get_session() as session:
                paid_ok = db.user_has_purchased(
                    session, db_id,
                    message_id=msg_data["id"],
                    purchase_type="broadcast",
                )
        result = await _send_single(bot, tg_id, msg_data, db_id, already_paid=paid_ok)
        if result:
            success += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)

    # ── Groups (no payment) ───────────────────────────────────────────────
    for g in groups_data:
        result = await _send_single(bot, g["chat_id"], msg_data, None, already_paid=True)
        if result:
            success += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)

    # FIX: repeating jobs stay SCHEDULED
    is_repeating = bool(msg_data.get("interval_hours") and msg_data["interval_hours"] > 0)
    with get_session() as session:
        db.mark_broadcast_executed(session, message_id, is_repeating=is_repeating)

    return {
        "total":        total,
        "success":      success,
        "failed":       failed,
        "users_total":  len(users_data),
        "groups_total": len(groups_data),
    }


# ─── Deliver unlocked paid content after payment ──────────────────────────────

async def send_paid_content_to_user(
    bot: Bot,
    chat_id: int,
    user_db_id: int,
    message_id: int,
) -> bool:
    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            return False
        msg_data = _serialize_msg(msg)

    result = await _send_single(bot, chat_id, msg_data, user_db_id, already_paid=True)
    # FIX: was `result is not None` — always True for list; correct check is bool(result)
    return bool(result)


# ─── Auto-delete expired messages ─────────────────────────────────────────────

async def delete_expired_messages(bot: Bot):
    """Called every 30s by the scheduler."""
    now = datetime.datetime.utcnow()

    # SelfDestructJob table (covers welcome, start, broadcast)
    with get_session() as session:
        jobs = db.get_due_destruct_jobs(session, now)
    # FIX: session closed BEFORE network calls to avoid holding DB lock during I/O
    for job in jobs:
        try:
            await bot.delete_message(chat_id=job["chat_id"], message_id=job["message_id"])
            logger.debug(f"Auto-deleted msg {job['message_id']} in chat {job['chat_id']}")
        except TelegramError as e:
            logger.debug(f"Delete failed: {e}")
        with get_session() as session:
            db.mark_destruct_done(session, job["id"])

    # LEGACY: MessageView-based deletions
    with get_session() as session:
        views_data = []
        views = db.get_pending_deletions(session, now)
        for view in views:
            msg  = view.message
            user = view.user
            if not msg or not user or msg.destruct_mode != DestructMode.AFTER_TIME:
                continue
            deadline = view.viewed_at + datetime.timedelta(seconds=msg.destruct_seconds)
            if now < deadline:
                continue
            views_data.append({
                "id": view.id,
                "user_tg_id": user.telegram_id,
                "sent_msg_id": view.sent_msg_id,
            })
    # FIX: session closed before network calls
    for vd in views_data:
        try:
            await bot.delete_message(chat_id=vd["user_tg_id"], message_id=vd["sent_msg_id"])
        except TelegramError as e:
            logger.debug(f"Legacy delete failed: {e}")
        with get_session() as session:
            db.mark_view_deleted(session, vd["id"])
