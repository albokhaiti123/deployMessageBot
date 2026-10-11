"""
Unified message sender — sends any configured message with:
  • Single media or media group (multiple photos/videos)
  • protect_content
  • has_spoiler (for paid photo/video)
  • URL inline buttons (from buttons_json)
  • Self-destruct scheduling (via SelfDestructJob)

Used by: welcome messages, /start messages, and broadcast.
"""
import json
import logging
from typing import Optional

from telegram import (
    Bot, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument,
)
from telegram.error import TelegramError

from database.models import get_session
from database import db

logger = logging.getLogger(__name__)


def _build_keyboard(
    buttons: list[dict],
    extra_buttons: Optional[list[list[InlineKeyboardButton]]] = None,
) -> Optional[InlineKeyboardMarkup]:
    rows = []
    for btn in buttons:
        text = btn.get("text", "")
        url  = btn.get("url", "")
        if text and url:
            rows.append([InlineKeyboardButton(text=text, url=url)])
    if extra_buttons:
        rows.extend(extra_buttons)
    return InlineKeyboardMarkup(rows) if rows else None


def _make_input_media(item: dict, caption: str = None, spoiler: bool = False):
    """Convert a media_list item to InputMedia* for send_media_group."""
    t   = item.get("type", "photo")
    fid = item.get("file_id")
    if t == "photo":
        return InputMediaPhoto(media=fid, caption=caption, parse_mode="HTML", has_spoiler=spoiler)
    if t == "video":
        return InputMediaVideo(media=fid, caption=caption, parse_mode="HTML", has_spoiler=spoiler)
    if t == "audio":
        return InputMediaAudio(media=fid, caption=caption, parse_mode="HTML")
    if t == "document":
        return InputMediaDocument(media=fid, caption=caption, parse_mode="HTML")
    return InputMediaPhoto(media=fid, caption=caption, parse_mode="HTML")


async def send_configured_message(
    bot: Bot,
    chat_id: int,
    *,
    text: Optional[str] = None,
    media_type: str = "none",
    media_file_id: Optional[str] = None,
    media_list: Optional[list[dict]] = None,
    protect_content: bool = False,
    has_spoiler: bool = False,
    buttons: Optional[list[dict]] = None,
    extra_buttons: Optional[list[list[InlineKeyboardButton]]] = None,
    destruct_seconds: int = 0,
) -> Optional[int]:
    """
    Send a message and optionally schedule self-destruct.
    Returns the Telegram message_id of the LAST sent message, or None on failure.
    
    Priority: media_list (multi) > media_type+media_file_id (single) > text only
    """
    kb       = _build_keyboard(buttons or [], extra_buttons)
    sent_ids = []

    try:
        # ── Multi-media (media group) ─────────────────────────────────────
        if media_list and len(media_list) > 1:
            group = []
            for i, item in enumerate(media_list):
                cap = text if i == 0 else None  # caption on first item only
                group.append(_make_input_media(item, caption=cap, spoiler=has_spoiler))

            results = await bot.send_media_group(
                chat_id=chat_id,
                media=group,
                protect_content=protect_content,
            )
            sent_ids = [m.message_id for m in results]

            # send_media_group can't attach buttons, so send buttons separately
            if kb:
                btn_msg = await bot.send_message(
                    chat_id=chat_id,
                    text="⬆️",
                    protect_content=protect_content,
                    reply_markup=kb,
                )
                sent_ids.append(btn_msg.message_id)

        # ── Single media (from media_list[0] or legacy fields) ────────────
        elif media_list and len(media_list) == 1:
            item = media_list[0]
            sent = await _send_single_media(
                bot, chat_id, item["type"], item["file_id"],
                text, protect_content, has_spoiler, kb,
            )
            if sent:
                sent_ids.append(sent.message_id)

        elif media_type != "none" and media_file_id:
            sent = await _send_single_media(
                bot, chat_id, media_type, media_file_id,
                text, protect_content, has_spoiler, kb,
            )
            if sent:
                sent_ids.append(sent.message_id)

        # ── Text only ─────────────────────────────────────────────────────
        else:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=text or "—",
                parse_mode="HTML",
                protect_content=protect_content,
                reply_markup=kb,
            )
            sent_ids.append(sent.message_id)

    except TelegramError as e:
        logger.warning(f"send_configured_message to {chat_id} failed: {e}")
        return None

    if not sent_ids:
        return None

    # Schedule self-destruct for ALL sent messages
    if destruct_seconds > 0:
        with get_session() as session:
            for mid in sent_ids:
                db.schedule_destruct(session, chat_id, mid, destruct_seconds)

    return sent_ids[-1]  # return last message_id


async def _send_single_media(bot, chat_id, media_type, file_id, text, protect, spoiler, kb):
    """Send a single media item. Returns the sent Message or None."""
    if media_type == "photo":
        return await bot.send_photo(
            chat_id=chat_id, photo=file_id, caption=text,
            parse_mode="HTML", has_spoiler=spoiler,
            protect_content=protect, reply_markup=kb,
        )
    if media_type == "video":
        return await bot.send_video(
            chat_id=chat_id, video=file_id, caption=text,
            parse_mode="HTML", has_spoiler=spoiler,
            protect_content=protect, reply_markup=kb,
        )
    if media_type == "audio":
        return await bot.send_audio(
            chat_id=chat_id, audio=file_id, caption=text,
            parse_mode="HTML", protect_content=protect, reply_markup=kb,
        )
    if media_type == "document":
        return await bot.send_document(
            chat_id=chat_id, document=file_id, caption=text,
            parse_mode="HTML", protect_content=protect, reply_markup=kb,
        )
    return None
