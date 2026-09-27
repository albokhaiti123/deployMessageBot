"""
Broadcast service: sends a BroadcastMessage to all active users.

Paid messages:
  - Photo/Video → sent with has_spoiler=True (blurred) + payment button
  - Text only   → sent with a lock notice + payment button
  - After payment → actual content re-sent without spoiler
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

# ─── Keyboards ────────────────────────────────────────────────────────────────

def _make_paid_keyboard(message_id: int, stars: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🔓 فتح المحتوى — {stars} ⭐",
            callback_data=f"pay_{message_id}"
        )
    ]])


def _make_destruct_keyboard(view_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تم المشاهدة — احذف الرسالة", callback_data=f"viewed_{view_id}")
    ]])


# ─── Sending logic ────────────────────────────────────────────────────────────

async def _send_single(
    bot: Bot,
    chat_id: int,
    msg: BroadcastMessage,
    user_db_id: int,
    session,
) -> Optional[int]:
    """
    Send one message to one user.
    Returns the Telegram message_id of what was sent, or None on failure.
    """
    protect = msg.protect_content
    sent_message = None

    # ── PAID: user hasn't purchased yet → send blurred teaser ─────────────────
    if msg.is_paid and not db.user_has_purchased(session, user_db_id, msg.id):
        pay_kb = _make_paid_keyboard(msg.id, msg.stars_price)
        try:
            if msg.media_type == MediaType.PHOTO and msg.media_file_id:
                # Blurred spoiler image + pay button underneath
                sent_message = await bot.send_photo(
                    chat_id=chat_id,
                    photo=msg.media_file_id,
                    caption=(
                        f"🔒 <b>محتوى مدفوع</b>\n\n"
                        f"{msg.text or ''}\n\n"
                        f"⭐ افتح المحتوى مقابل <b>{msg.stars_price} نجمة</b>"
                    ).strip(),
                    parse_mode="HTML",
                    has_spoiler=True,        # ← blurred / hidden until tapped
                    protect_content=protect,
                    reply_markup=pay_kb,
                )
            elif msg.media_type == MediaType.VIDEO and msg.media_file_id:
                sent_message = await bot.send_video(
                    chat_id=chat_id,
                    video=msg.media_file_id,
                    caption=(
                        f"🔒 <b>محتوى مدفوع</b>\n\n"
                        f"{msg.text or ''}\n\n"
                        f"⭐ افتح المحتوى مقابل <b>{msg.stars_price} نجمة</b>"
                    ).strip(),
                    parse_mode="HTML",
                    has_spoiler=True,        # ← blurred video thumbnail
                    protect_content=protect,
                    reply_markup=pay_kb,
                )
            else:
                # Text-only paid teaser
                sent_message = await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔒 <b>محتوى مدفوع</b>\n\n"
                        f"{msg.text[:200] + '...' if msg.text and len(msg.text) > 200 else (msg.text or '')}\n\n"
                        f"⭐ افتح المحتوى مقابل <b>{msg.stars_price} نجمة</b>"
                    ).strip(),
                    parse_mode="HTML",
                    protect_content=protect,
                    reply_markup=pay_kb,
                )
        except (TelegramError, Forbidden) as e:
            logger.warning(f"Cannot send paid teaser to {chat_id}: {e}")
            return None
        return sent_message.message_id if sent_message else None

    # ── FREE (or already paid) → send actual content ───────────────────────────
    reply_markup = None   # filled below for immediate destruct

    try:
        if msg.media_type == MediaType.NONE or not msg.media_file_id:
            sent_message = await bot.send_message(
                chat_id=chat_id,
                text=msg.text or "—",
                parse_mode="HTML",
                protect_content=protect,
                reply_markup=reply_markup,
            )
        elif msg.media_type == MediaType.PHOTO:
            sent_message = await bot.send_photo(
                chat_id=chat_id,
                photo=msg.media_file_id,
                caption=msg.text,
                parse_mode="HTML",
                protect_content=protect,
                reply_markup=reply_markup,
            )
        elif msg.media_type == MediaType.VIDEO:
            sent_message = await bot.send_video(
                chat_id=chat_id,
                video=msg.media_file_id,
                caption=msg.text,
                parse_mode="HTML",
                protect_content=protect,
                reply_markup=reply_markup,
            )
        elif msg.media_type == MediaType.AUDIO:
            sent_message = await bot.send_audio(
                chat_id=chat_id,
                audio=msg.media_file_id,
                caption=msg.text,
                parse_mode="HTML",
                protect_content=protect,
                reply_markup=reply_markup,
            )
        elif msg.media_type == MediaType.DOCUMENT:
            sent_message = await bot.send_document(
                chat_id=chat_id,
                document=msg.media_file_id,
                caption=msg.text,
                parse_mode="HTML",
                protect_content=protect,
                reply_markup=reply_markup,
            )
    except (TelegramError, Forbidden) as e:
        logger.warning(f"Cannot send content to {chat_id}: {e}")
        return None

    if not sent_message:
        return None

    sent_msg_id = sent_message.message_id

    # Record view
    view = db.record_view(session, user_db_id, msg.id, sent_msg_id)

    # Attach "viewed" button for IMMEDIATE self-destruct
    if msg.destruct_mode == DestructMode.IMMEDIATE:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=sent_msg_id,
                reply_markup=_make_destruct_keyboard(view.id),
            )
        except TelegramError:
            pass

    return sent_msg_id


# ─── Public: broadcast to all users ──────────────────────────────────────────

async def broadcast(bot: Bot, message_id: int) -> dict:
    """Broadcast to all active subscribers. Returns stats dict."""
    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            return {"error": "Message not found"}

        users = db.get_all_active_users(session)

    total = len(users)
    success = 0
    failed = 0

    # Re-open session per-user to avoid long transactions
    for user in users:
        with get_session() as session:
            result = await _send_single(bot, user.telegram_id, msg, user.id, session)
        if result is not None:
            success += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)   # ~20 msg/sec, safe under Telegram limits

    with get_session() as session:
        db.update_message_status(session, message_id, MessageStatus.SENT)

    return {"total": total, "success": success, "failed": failed}


# ─── Public: deliver paid content after payment ───────────────────────────────

async def send_paid_content_to_user(
    bot: Bot,
    chat_id: int,
    user_db_id: int,
    message_id: int,
) -> bool:
    """Send the real (unlocked) content to a user who just paid."""
    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            return False
        sent_id = await _send_single(bot, chat_id, msg, user_db_id, session)
    return sent_id is not None


# ─── Scheduled cleanup: delete time-expired messages ─────────────────────────

async def delete_expired_messages(bot: Bot):
    now = datetime.datetime.utcnow()
    with get_session() as session:
        views = db.get_pending_deletions(session, now)
        for view in views:
            msg = view.message
            if msg.destruct_mode == DestructMode.AFTER_TIME:
                deadline = view.viewed_at + datetime.timedelta(seconds=msg.destruct_seconds)
                if now < deadline:
                    continue
            # Skip IMMEDIATE — those are handled by the "viewed" callback button
            if msg.destruct_mode == DestructMode.IMMEDIATE:
                continue

            user = view.user
            try:
                await bot.delete_message(
                    chat_id=user.telegram_id,
                    message_id=view.sent_msg_id,
                )
            except TelegramError as e:
                logger.debug(f"Delete expired msg failed: {e}")
            db.mark_view_deleted(session, view.id)
