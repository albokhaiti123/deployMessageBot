"""
Unified message sender — sends any configured message with:
  • Any media type (text / photo / video / audio / document)
  • protect_content
  • has_spoiler (for paid photo/video)
  • URL inline buttons (from buttons_json)
  • Self-destruct scheduling (via SelfDestructJob)

Used by: welcome messages, /start messages, and broadcast.
"""
import json
import logging
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from database.models import get_session
from database import db

logger = logging.getLogger(__name__)


def _build_keyboard(
    buttons: list[dict],
    extra_buttons: Optional[list[list[InlineKeyboardButton]]] = None,
) -> Optional[InlineKeyboardMarkup]:
    """
    Build InlineKeyboardMarkup from buttons list + optional extra button rows.
    buttons: [{"text": "...", "url": "..."}, ...]
    extra_buttons: [[InlineKeyboardButton(...)], ...]  (e.g. pay button)
    """
    rows = []
    # URL buttons from config — each button on its own row
    for btn in buttons:
        text = btn.get("text", "")
        url  = btn.get("url", "")
        if text and url:
            rows.append([InlineKeyboardButton(text=text, url=url)])

    # Extra rows (e.g. pay button)
    if extra_buttons:
        rows.extend(extra_buttons)

    return InlineKeyboardMarkup(rows) if rows else None


async def send_configured_message(
    bot: Bot,
    chat_id: int,
    *,
    text: Optional[str] = None,
    media_type: str = "none",
    media_file_id: Optional[str] = None,
    protect_content: bool = False,
    has_spoiler: bool = False,
    buttons: Optional[list[dict]] = None,
    extra_buttons: Optional[list[list[InlineKeyboardButton]]] = None,
    destruct_seconds: int = 0,
) -> Optional[int]:
    """
    Send a message and optionally schedule self-destruct.
    Returns the Telegram message_id of what was sent, or None on failure.
    """
    kb   = _build_keyboard(buttons or [], extra_buttons)
    sent = None

    try:
        if media_type == "none" or not media_file_id:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=text or "—",
                parse_mode="HTML",
                protect_content=protect_content,
                reply_markup=kb,
            )
        elif media_type == "photo":
            sent = await bot.send_photo(
                chat_id=chat_id,
                photo=media_file_id,
                caption=text,
                parse_mode="HTML",
                has_spoiler=has_spoiler,
                protect_content=protect_content,
                reply_markup=kb,
            )
        elif media_type == "video":
            sent = await bot.send_video(
                chat_id=chat_id,
                video=media_file_id,
                caption=text,
                parse_mode="HTML",
                has_spoiler=has_spoiler,
                protect_content=protect_content,
                reply_markup=kb,
            )
        elif media_type == "audio":
            sent = await bot.send_audio(
                chat_id=chat_id,
                audio=media_file_id,
                caption=text,
                parse_mode="HTML",
                protect_content=protect_content,
                reply_markup=kb,
            )
        elif media_type == "document":
            sent = await bot.send_document(
                chat_id=chat_id,
                document=media_file_id,
                caption=text,
                parse_mode="HTML",
                protect_content=protect_content,
                reply_markup=kb,
            )
    except TelegramError as e:
        logger.warning(f"send_configured_message to {chat_id} failed: {e}")
        return None

    if not sent:
        return None

    # Schedule self-destruct if needed
    if destruct_seconds > 0:
        with get_session() as session:
            db.schedule_destruct(session, chat_id, sent.message_id, destruct_seconds)

    return sent.message_id
