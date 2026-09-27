"""
Broadcast service: sends a BroadcastMessage to all active users.
Handles free vs paid messages and protect_content.
"""
import asyncio
import logging
import datetime
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from telegram.error import TelegramError, Forbidden

from database.models import (
    get_session, BroadcastMessage, MediaType, DestructMode, MessageStatus
)
from database import db

logger = logging.getLogger(__name__)

# ─── Preview text for paid (blurred) messages ─────────────────────────────────
PAID_PREVIEW_TEXT = (
    "🔒 <b>محتوى مدفوع</b>\n\n"
    "هذا المحتوى متاح بعد الدفع.\n"
    "اضغط على زر <b>فتح المحتوى</b> للمتابعة."
)


def _make_paid_keyboard(message_id: int, stars: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔓 فتح المحتوى ⭐ {stars} نجمة",
            callback_data=f"pay_{message_id}"
        )]
    ])


def _make_destruct_keyboard(view_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تم المشاهدة - احذف الرسالة", callback_data=f"viewed_{view_id}")]
    ])


async def _send_single(
    bot: Bot,
    chat_id: int,
    msg: BroadcastMessage,
    user_db_id: int,
    session,
) -> Optional[int]:
    """
    Sends the message to one user. Returns the sent Telegram message_id or None.
    For paid messages that haven't been purchased, sends a teaser instead.
    """
    protect = msg.protect_content
    sent_message = None

    # ── Paid: check if user already purchased ─────────────────────────────────
    if msg.is_paid:
        already_paid = db.user_has_purchased(session, user_db_id, msg.id)
        if not already_paid:
            # Send teaser / lock notice
            try:
                sent_message = await bot.send_message(
                    chat_id=chat_id,
                    text=PAID_PREVIEW_TEXT,
                    parse_mode="HTML",
                    protect_content=protect,
                    reply_markup=_make_paid_keyboard(msg.id, msg.stars_price),
                )
            except (TelegramError, Forbidden) as e:
                logger.warning(f"Cannot send to {chat_id}: {e}")
                return None
            return sent_message.message_id

    # ── Determine reply markup ─────────────────────────────────────────────────
    reply_markup = None
    if msg.destruct_mode == DestructMode.IMMEDIATE:
        # We'll attach a "viewed" button; actual view_id set after saving record
        # We'll update keyboard after recording
        pass

    # ── Send based on media type ───────────────────────────────────────────────
    try:
        if msg.media_type == MediaType.NONE or not msg.media_file_id:
            # Text only
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
        logger.warning(f"Cannot send to {chat_id}: {e}")
        return None

    if not sent_message:
        return None

    sent_msg_id = sent_message.message_id

    # ── Record view ────────────────────────────────────────────────────────────
    view = db.record_view(session, user_db_id, msg.id, sent_msg_id)

    # ── Attach "viewed" button for IMMEDIATE destruct ──────────────────────────
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


async def broadcast(bot: Bot, message_id: int) -> dict:
    """
    Broadcast a BroadcastMessage to all active users.
    Returns stats dict: {total, success, failed}.
    """
    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            return {"error": "Message not found"}

        users = db.get_all_active_users(session)
        total = len(users)
        success = 0
        failed = 0

        for user in users:
            result = await _send_single(bot, user.telegram_id, msg, user.id, session)
            if result is not None:
                success += 1
            else:
                failed += 1
            # Rate limit: max ~30 messages/sec to group of users
            await asyncio.sleep(0.05)

        db.update_message_status(session, message_id, MessageStatus.SENT)

        return {"total": total, "success": success, "failed": failed}


async def send_paid_content_to_user(
    bot: Bot,
    chat_id: int,
    user_db_id: int,
    message_id: int,
) -> bool:
    """
    Sends the actual paid content to a user after successful payment.
    """
    with get_session() as session:
        msg = db.get_message_by_id(session, message_id)
        if not msg:
            return False
        sent_id = await _send_single(bot, chat_id, msg, user_db_id, session)
        return sent_id is not None


async def delete_expired_messages(bot: Bot):
    """
    Called periodically by scheduler. Deletes messages past their destruct time.
    """
    now = datetime.datetime.utcnow()
    with get_session() as session:
        views = db.get_pending_deletions(session, now)
        for view in views:
            msg = view.message
            should_delete = False

            if msg.destruct_mode == DestructMode.AFTER_TIME:
                deadline = view.viewed_at + datetime.timedelta(seconds=msg.destruct_seconds)
                if now >= deadline:
                    should_delete = True
            # IMMEDIATE is handled via callback button in _send_single

            if should_delete:
                user = view.user
                try:
                    await bot.delete_message(
                        chat_id=user.telegram_id,
                        message_id=view.sent_msg_id,
                    )
                except TelegramError as e:
                    logger.debug(f"Could not delete message: {e}")
                db.mark_view_deleted(session, view.id)
