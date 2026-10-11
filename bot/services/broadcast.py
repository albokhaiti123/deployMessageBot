"""
Broadcast service — uses the unified message_sender.

Supports:
  • Users / Groups / All targets
  • Paid messages with spoiler + pay button
  • URL buttons from buttons_json
  • Self-destruct via SelfDestructJob table
"""
import asyncio
import json
import logging
import datetime
from typing import Optional, Literal

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, Forbidden

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


async def _send_single(
    bot: Bot,
    chat_id: int,
    msg_data: dict,
    user_db_id: Optional[int],
    already_paid: bool = False,
) -> Optional[int]:
    """Send one message to one chat using the unified sender."""
    is_paid  = msg_data["is_paid"] and not already_paid and user_db_id is not None
    spoiler  = is_paid and msg_data["media_type"] in ("photo", "video")
    extra    = _pay_kb(msg_data["id"], msg_data["stars_price"]) if is_paid else None
    buttons  = msg_data.get("buttons", [])

    return await send_configured_message(
        bot,
        chat_id,
        text=msg_data["text"],
        media_type=msg_data["media_type"],
        media_file_id=msg_data["media_file_id"],
        media_list=msg_data.get("media_list", []),
        protect_content=msg_data["protect_content"],
        has_spoiler=spoiler,
        buttons=buttons,
        extra_buttons=extra,
        destruct_seconds=msg_data["destruct_seconds"] if not is_paid else 0,
    )


def _serialize_msg(msg: BroadcastMessage) -> dict:
    """Serialize ORM object to a plain dict inside a session."""
    media_list = json.loads(msg.media_json or "[]") if hasattr(msg, 'media_json') and msg.media_json else []
    # Backward compat
    if not media_list and msg.media_file_id and msg.media_type and msg.media_type != MediaType.NONE:
        media_list = [{"type": msg.media_type.value, "file_id": msg.media_file_id}]
    return {
        "id":             msg.id,
        "text":           msg.text,
        "media_type":     msg.media_type.value if msg.media_type else "none",
        "media_file_id":  msg.media_file_id,
        "media_list":     media_list,
        "is_paid":        msg.is_paid,
        "stars_price":    msg.stars_price or 0,
        "protect_content":msg.protect_content,
        "destruct_mode":  msg.destruct_mode.value if msg.destruct_mode else "never",
        "destruct_seconds": msg.destruct_seconds or 0,
        "buttons":        json.loads(msg.buttons_json or "[]"),
    }


# ─── Main broadcast ───────────────────────────────────────────────────────────

async def broadcast(
    bot: Bot,
    message_id: int,
    target: BroadcastTarget = "users",
) -> dict:
    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            return {"error": "Message not found"}
        msg_data    = _serialize_msg(msg)
        users_data  = []
        groups_data = []
        if target in ("users", "all"):
            users = db.get_all_active_users(session)
            users_data = [(u.telegram_id, u.id) for u in users]
        if target in ("groups", "all"):
            groups_data = db.get_active_groups(session)

    total   = len(users_data) + len(groups_data)
    success = failed = 0

    # Users
    for tg_id, db_id in users_data:
        paid_ok = False
        if msg_data["is_paid"]:
            with get_session() as session:
                paid_ok = db.user_has_purchased(session, db_id, msg_data["id"])
        result = await _send_single(bot, tg_id, msg_data, db_id, already_paid=paid_ok)
        if result:
            success += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)

    # Groups (no payment for groups)
    for g in groups_data:
        result = await _send_single(bot, g["chat_id"], msg_data, None, already_paid=True)
        if result:
            success += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)

    with get_session() as session:
        db.update_message_status(session, message_id, MessageStatus.SENT)

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
    return result is not None


# ─── Auto-delete expired messages (legacy broadcast views + new SelfDestructJob)

async def delete_expired_messages(bot: Bot):
    """Called every 30s by the scheduler."""
    now = datetime.datetime.utcnow()

    # NEW: SelfDestructJob table (covers welcome, start, broadcast)
    with get_session() as session:
        jobs = db.get_due_destruct_jobs(session, now)
    for job in jobs:
        try:
            await bot.delete_message(chat_id=job["chat_id"], message_id=job["message_id"])
            logger.debug(f"Auto-deleted msg {job['message_id']} in chat {job['chat_id']}")
        except TelegramError as e:
            logger.debug(f"Delete failed: {e}")
        with get_session() as session:
            db.mark_destruct_done(session, job["id"])

    # LEGACY: MessageView-based deletions (for old broadcast messages)
    with get_session() as session:
        views = db.get_pending_deletions(session, now)
        for view in views:
            msg  = view.message
            user = view.user
            if msg.destruct_mode != DestructMode.AFTER_TIME:
                continue
            deadline = view.viewed_at + datetime.timedelta(seconds=msg.destruct_seconds)
            if now < deadline:
                continue
            try:
                await bot.delete_message(chat_id=user.telegram_id, message_id=view.sent_msg_id)
            except TelegramError as e:
                logger.debug(f"Legacy delete failed: {e}")
            db.mark_view_deleted(session, view.id)
