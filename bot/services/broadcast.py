"""
Broadcast service — raw delivery to users and/or groups.

Broadcast targets:
  "users"  → all subscribed private-chat users
  "groups" → all active groups the bot is in
  "all"    → both users and groups

Paid content:
  • Photo / Video → has_spoiler=True (blurred) + original caption + pay button only
  • Text / Audio / Document → original content + pay button only
  • After payment → resend without spoiler, without button

Self-destruct: fully automatic via scheduler (no user button).
"""
import asyncio
import logging
import datetime
from typing import Optional, Literal

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, Forbidden

from database.models import (
    get_session, BroadcastMessage, MediaType, DestructMode, MessageStatus
)
from database import db

logger = logging.getLogger(__name__)

BroadcastTarget = Literal["users", "groups", "all"]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _pay_kb(message_id: int, stars: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🔓 فتح المحتوى — {stars} ⭐",
            callback_data=f"pay_{message_id}"
        )
    ]])


async def _send_to_chat(
    bot: Bot,
    chat_id: int,
    msg: BroadcastMessage,
    user_db_id: Optional[int],   # None for groups
    session,
    already_paid: bool = False,
) -> Optional[int]:
    """
    Send one message to one chat (user or group).
    Returns Telegram message_id or None on failure.
    """
    protect  = msg.protect_content
    is_paid  = msg.is_paid and not already_paid and user_db_id is not None
    kb       = _pay_kb(msg.id, msg.stars_price) if is_paid else None
    # Blur photo/video for unpaid messages
    spoiler  = is_paid and msg.media_type in (MediaType.PHOTO, MediaType.VIDEO)

    sent = None
    try:
        mtype = msg.media_type

        if mtype == MediaType.NONE or not msg.media_file_id:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=msg.text or "—",
                parse_mode="HTML",
                protect_content=protect,
                reply_markup=kb,
            )
        elif mtype == MediaType.PHOTO:
            sent = await bot.send_photo(
                chat_id=chat_id,
                photo=msg.media_file_id,
                caption=msg.text,
                parse_mode="HTML",
                has_spoiler=spoiler,
                protect_content=protect,
                reply_markup=kb,
            )
        elif mtype == MediaType.VIDEO:
            sent = await bot.send_video(
                chat_id=chat_id,
                video=msg.media_file_id,
                caption=msg.text,
                parse_mode="HTML",
                has_spoiler=spoiler,
                protect_content=protect,
                reply_markup=kb,
            )
        elif mtype == MediaType.AUDIO:
            sent = await bot.send_audio(
                chat_id=chat_id,
                audio=msg.media_file_id,
                caption=msg.text,
                parse_mode="HTML",
                protect_content=protect,
                reply_markup=kb,
            )
        elif mtype == MediaType.DOCUMENT:
            sent = await bot.send_document(
                chat_id=chat_id,
                document=msg.media_file_id,
                caption=msg.text,
                parse_mode="HTML",
                protect_content=protect,
                reply_markup=kb,
            )

    except (TelegramError, Forbidden) as e:
        logger.warning(f"Cannot send to {chat_id}: {e}")
        return None

    if not sent:
        return None

    # Record delivery for self-destruct timer (users only)
    if user_db_id is not None and msg.destruct_mode == DestructMode.AFTER_TIME and msg.destruct_seconds > 0:
        db.record_view(session, user_db_id, msg.id, sent.message_id)

    return sent.message_id


# ─── Main broadcast ───────────────────────────────────────────────────────────

async def broadcast(
    bot: Bot,
    message_id: int,
    target: BroadcastTarget = "users",
) -> dict:
    """
    Broadcast message to the selected target.
    Returns stats dict: {total, success, failed, users_total, groups_total}
    """
    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            return {"error": "Message not found"}

        msg_id      = msg.id
        is_paid     = msg.is_paid
        dest_mode   = msg.destruct_mode
        dest_secs   = msg.destruct_seconds

        users_data  = []
        groups_data = []

        if target in ("users", "all"):
            users = db.get_all_active_users(session)
            users_data = [(u.telegram_id, u.id) for u in users]

        if target in ("groups", "all"):
            groups_data = db.get_active_groups(session)   # list of dicts

    total   = len(users_data) + len(groups_data)
    success = failed = 0

    # ── Send to users ─────────────────────────────────────────────────────────
    for tg_id, db_id in users_data:
        with get_session() as session:
            msg_obj    = db.get_message_by_id(session, msg_id)
            paid_ok    = is_paid and db.user_has_purchased(session, db_id, msg_id)
            result     = await _send_to_chat(
                bot, tg_id, msg_obj, db_id, session, already_paid=paid_ok
            )
        if result:
            success += 1
        else:
            failed  += 1
        await asyncio.sleep(0.05)

    # ── Send to groups ────────────────────────────────────────────────────────
    for g in groups_data:
        with get_session() as session:
            msg_obj = db.get_message_by_id(session, msg_id)
            # Groups never have paid content gates (no user_db_id)
            result  = await _send_to_chat(
                bot, g["chat_id"], msg_obj, None, session, already_paid=True
            )
        if result:
            success += 1
        else:
            failed  += 1
        await asyncio.sleep(0.05)

    with get_session() as session:
        db.update_message_status(session, msg_id, MessageStatus.SENT)

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
    """Send real (no spoiler, no button) content after Stars payment."""
    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            return False
        result = await _send_to_chat(
            bot, chat_id, msg, user_db_id, session, already_paid=True
        )
    return result is not None


# ─── Auto-delete expired messages ─────────────────────────────────────────────

async def delete_expired_messages(bot: Bot):
    """Called every 30s. Deletes user messages past their destruct deadline."""
    now = datetime.datetime.utcnow()
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
                await bot.delete_message(
                    chat_id=user.telegram_id,
                    message_id=view.sent_msg_id,
                )
                logger.debug(f"Auto-deleted msg {view.sent_msg_id} for {user.telegram_id}")
            except TelegramError as e:
                logger.debug(f"Delete failed: {e}")
            db.mark_view_deleted(session, view.id)
