"""
Broadcast service — sends messages exactly as the admin created them.

Rules:
  • Free  → send raw, zero additions
  • Paid  → photo/video with has_spoiler=True + original caption + pay button only
            text/audio/doc → original text + pay button only
  • After payment → resend without spoiler, no button
  • Self-destruct → fully automatic; the scheduler deletes after destruct_seconds
                    from delivery time.  No user-facing "viewed" button.
"""
import asyncio
import logging
import datetime
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, Forbidden

from database.models import (
    get_session, BroadcastMessage, MediaType, DestructMode, MessageStatus
)
from database import db

logger = logging.getLogger(__name__)


# ─── Payment keyboard (only addition for paid messages) ───────────────────────

def _pay_kb(message_id: int, stars: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🔓 فتح المحتوى — {stars} ⭐",
            callback_data=f"pay_{message_id}"
        )
    ]])


# ─── Core send ────────────────────────────────────────────────────────────────

async def _send_single(
    bot: Bot,
    chat_id: int,
    msg: BroadcastMessage,
    user_db_id: int,
    session,
    already_paid: bool = False,
) -> Optional[int]:
    """
    Send one message to one user.
    Returns the sent Telegram message_id, or None on failure.
    """
    protect  = msg.protect_content
    is_paid  = msg.is_paid and not already_paid
    kb       = _pay_kb(msg.id, msg.stars_price) if is_paid else None
    # For paid media: blur it so content stays hidden until payment
    spoiler  = is_paid and msg.media_type in (MediaType.PHOTO, MediaType.VIDEO)

    sent = None
    try:
        mtype = msg.media_type

        if mtype == MediaType.NONE or not msg.media_file_id:
            # ── Plain text ───────────────────────────────────────────────────
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

    # Record delivery (for self-destruct timer)
    if msg.destruct_mode != DestructMode.NEVER and msg.destruct_seconds > 0:
        db.record_view(session, user_db_id, msg.id, sent.message_id)

    return sent.message_id


# ─── Public: broadcast to all subscribers ────────────────────────────────────

async def broadcast(bot: Bot, message_id: int) -> dict:
    """Broadcast to all active users. Returns stats."""
    # Load message data once
    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            return {"error": "Message not found"}
        # Detach: read all needed attributes while session is open
        msg_id       = msg.id
        is_paid      = msg.is_paid
        protect      = msg.protect_content
        media_type   = msg.media_type
        media_fid    = msg.media_file_id
        text         = msg.text
        stars        = msg.stars_price
        dest_mode    = msg.destruct_mode
        dest_secs    = msg.destruct_seconds

        users = db.get_all_active_users(session)
        user_data = [(u.telegram_id, u.id) for u in users]

    total = len(user_data)
    success = failed = 0

    for tg_id, db_id in user_data:
        with get_session() as session:
            # Reload message inside session for ORM operations
            msg_obj = db.get_message_by_id(session, msg_id)
            if not msg_obj:
                failed += 1
                continue
            paid_ok = is_paid and db.user_has_purchased(session, db_id, msg_id)
            result = await _send_single(
                bot, tg_id, msg_obj, db_id, session,
                already_paid=paid_ok,
            )
        if result is not None:
            success += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)   # ≈20 msg/sec

    with get_session() as session:
        db.update_message_status(session, msg_id, MessageStatus.SENT)

    return {"total": total, "success": success, "failed": failed}


# ─── Public: send unlocked content after payment ──────────────────────────────

async def send_paid_content_to_user(
    bot: Bot,
    chat_id: int,
    user_db_id: int,
    message_id: int,
) -> bool:
    """Send the unlocked (no spoiler, no pay button) content after payment."""
    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            return False
        # already_paid=True → no spoiler, no button
        result = await _send_single(
            bot, chat_id, msg, user_db_id, session, already_paid=True
        )
    return result is not None


# ─── Scheduler: delete messages past their deadline ──────────────────────────

async def delete_expired_messages(bot: Bot):
    """
    Called every 30 s by the scheduler.
    Deletes any delivered message whose deadline has passed.
    """
    now = datetime.datetime.utcnow()
    with get_session() as session:
        views = db.get_pending_deletions(session, now)
        for view in views:
            msg   = view.message
            user  = view.user

            if msg.destruct_mode == DestructMode.NEVER:
                continue

            if msg.destruct_mode == DestructMode.AFTER_TIME:
                deadline = view.viewed_at + datetime.timedelta(seconds=msg.destruct_seconds)
                if now < deadline:
                    continue

            try:
                await bot.delete_message(
                    chat_id=user.telegram_id,
                    message_id=view.sent_msg_id,
                )
                logger.debug(f"Auto-deleted msg {view.sent_msg_id} for user {user.telegram_id}")
            except TelegramError as e:
                logger.debug(f"Could not delete: {e}")
            db.mark_view_deleted(session, view.id)
