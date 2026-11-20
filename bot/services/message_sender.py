"""
Unified message sender — sends any configured message with:
  • Media group support (Telegram-compliant batches: max 10, compatible types)
  • Voice handled via send_voice (not send_audio)
  • Caption > 1024 chars overflow: send media without caption, then text separately
  • HTML parse error fallback to plain text (prevents full broadcast failure)
  • protect_content & has_spoiler
  • URL inline buttons (from buttons_json)
  • Self-destruct scheduling (via SelfDestructJob)
  • Button message isolated so media IDs are always saved even if buttons fail

Phase-1 fixes:
  - _partition_media_batches: enforces 10-item limit, groups compatible types
  - _send_single_media: added voice support via bot.send_voice
  - _is_html_parse_error + fallback to plain text
  - Caption length capped at MAX_CAPTION_LEN (1024)
  - Button send failure no longer swallows sent_ids
"""
import logging
from typing import Optional

from telegram import (
    Bot, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument,
)
from telegram.error import BadRequest, TelegramError

from database.models import get_session
from database import db

logger = logging.getLogger(__name__)

MAX_MEDIA_GROUP  = 10
MAX_CAPTION_LEN  = 1024
MAX_TEXT_LEN     = 4096


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


def _media_family(media_type: str) -> str:
    """Return Telegram media-group compatibility group."""
    if media_type in ("photo", "video"):
        return "visual"
    if media_type == "audio":
        return "audio"
    if media_type == "document":
        return "document"
    # voice / unknown → must be sent individually
    return "single_only"


def _partition_media_batches(media_list: list[dict]) -> list[list[dict]]:
    """
    Split media_list into Telegram-compliant batches:
      - photo/video share a group (≤ 10)
      - audio-only group (≤ 10)
      - document-only group (≤ 10)
      - voice is always individual (single_only)
    """
    batches: list[list[dict]] = []
    current_batch: list[dict] = []
    current_family: Optional[str] = None

    for item in media_list:
        fam = _media_family(item.get("type", "photo"))

        if fam == "single_only":
            if current_batch:
                batches.append(current_batch)
                current_batch = []
                current_family = None
            batches.append([item])
            continue

        if current_batch and (fam != current_family or len(current_batch) >= MAX_MEDIA_GROUP):
            batches.append(current_batch)
            current_batch = []

        current_batch.append(item)
        current_family = fam

    if current_batch:
        batches.append(current_batch)

    return batches


def _make_input_media(
    item: dict,
    caption: Optional[str] = None,
    spoiler: bool = False,
    parse_mode: Optional[str] = "HTML",
):
    """Convert a media_list item to InputMedia* for send_media_group."""
    t   = item.get("type", "photo")
    fid = item.get("file_id")
    if t == "photo":
        return InputMediaPhoto(media=fid, caption=caption, parse_mode=parse_mode, has_spoiler=spoiler)
    if t == "video":
        return InputMediaVideo(media=fid, caption=caption, parse_mode=parse_mode, has_spoiler=spoiler)
    if t == "audio":
        return InputMediaAudio(media=fid, caption=caption, parse_mode=parse_mode)
    if t == "document":
        return InputMediaDocument(media=fid, caption=caption, parse_mode=parse_mode)
    # fallback
    return InputMediaPhoto(media=fid, caption=caption, parse_mode=parse_mode)


def _is_html_parse_error(exc: BadRequest) -> bool:
    return "can't parse entities" in str(exc).lower()


async def _send_text_message(
    bot: Bot,
    chat_id: int,
    text: str,
    protect_content: bool,
    kb: Optional[InlineKeyboardMarkup],
) -> list[int]:
    """Send text (chunked if > 4096) with automatic plain-text fallback on HTML error."""
    ids: list[int] = []
    chunks = [text[i:i + MAX_TEXT_LEN] for i in range(0, len(text), MAX_TEXT_LEN)] or ["—"]
    for idx, chunk in enumerate(chunks):
        chunk_kb = kb if idx == len(chunks) - 1 else None
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode="HTML",
                protect_content=protect_content,
                reply_markup=chunk_kb,
            )
        except BadRequest as e:
            if not _is_html_parse_error(e):
                raise
            logger.warning(f"HTML parse failed for chat {chat_id}; retrying as plain text.")
            sent = await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=None,
                protect_content=protect_content,
                reply_markup=chunk_kb,
            )
        ids.append(sent.message_id)
    return ids


async def _send_single_media(
    bot: Bot,
    chat_id: int,
    media_type: str,
    file_id: str,
    caption: Optional[str],
    protect: bool,
    spoiler: bool,
    kb: Optional[InlineKeyboardMarkup],
    parse_mode: Optional[str] = "HTML",
):
    """Send a single media item with HTML→plain fallback. Returns Message or None."""

    async def _dispatch(pm: Optional[str]):
        if media_type == "photo":
            return await bot.send_photo(
                chat_id=chat_id, photo=file_id, caption=caption,
                parse_mode=pm, has_spoiler=spoiler,
                protect_content=protect, reply_markup=kb,
            )
        if media_type == "video":
            return await bot.send_video(
                chat_id=chat_id, video=file_id, caption=caption,
                parse_mode=pm, has_spoiler=spoiler,
                protect_content=protect, reply_markup=kb,
            )
        if media_type == "audio":
            return await bot.send_audio(
                chat_id=chat_id, audio=file_id, caption=caption,
                parse_mode=pm, protect_content=protect, reply_markup=kb,
            )
        # FIX: voice must use send_voice, not send_audio
        if media_type == "voice":
            return await bot.send_voice(
                chat_id=chat_id, voice=file_id, caption=caption,
                parse_mode=pm, protect_content=protect, reply_markup=kb,
            )
        if media_type == "document":
            return await bot.send_document(
                chat_id=chat_id, document=file_id, caption=caption,
                parse_mode=pm, protect_content=protect, reply_markup=kb,
            )
        return None

    try:
        return await _dispatch(parse_mode)
    except BadRequest as e:
        if parse_mode and _is_html_parse_error(e):
            logger.warning(f"HTML caption failed for chat {chat_id}; retrying plain text.")
            return await _dispatch(None)
        raise


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
) -> list[int]:
    """
    Send a message and optionally schedule self-destruct.
    Returns list of ALL Telegram message_ids sent, or empty list on failure.
    """
    kb = _build_keyboard(buttons or [], extra_buttons)
    sent_ids: list[int] = []

    # Normalize to effective_media list
    effective_media: list[dict] = []
    if media_list:
        effective_media = [m for m in media_list if m.get("file_id")]
    elif media_type and media_type != "none" and media_file_id:
        effective_media = [{"type": media_type, "file_id": media_file_id}]

    # FIX: caption overflow — if text > 1024 chars send media without caption
    text_overflow = bool(effective_media and text and len(text) > MAX_CAPTION_LEN)
    inline_caption = None if text_overflow else text

    try:
        if not effective_media:
            # ── Text only ─────────────────────────────────────────────────
            sent_ids.extend(
                await _send_text_message(bot, chat_id, text or "—", protect_content, kb)
            )

        else:
            batches = _partition_media_batches(effective_media)
            caption_attached = False
            kb_attached = False

            for batch_idx, batch in enumerate(batches):
                is_last_batch = (batch_idx == len(batches) - 1)
                cap_for_batch = inline_caption if (not caption_attached and inline_caption) else None

                if len(batch) == 1:
                    item = batch[0]
                    # Attach keyboard directly only if last batch and no overflow text pending
                    attach_kb = kb if (is_last_batch and not text_overflow) else None
                    try:
                        sent = await _send_single_media(
                            bot, chat_id, item["type"], item["file_id"],
                            cap_for_batch, protect_content, has_spoiler, attach_kb,
                        )
                        if sent:
                            sent_ids.append(sent.message_id)
                            if cap_for_batch:
                                caption_attached = True
                            if attach_kb:
                                kb_attached = True
                    except TelegramError as e:
                        logger.warning(f"Single media to {chat_id} failed: {e}")

                else:
                    # Media group (2–10 compatible items)
                    def _build_group(pm: Optional[str]) -> list:
                        return [
                            _make_input_media(
                                item,
                                caption=(cap_for_batch if i == 0 else None),
                                spoiler=has_spoiler,
                                parse_mode=pm,
                            )
                            for i, item in enumerate(batch)
                        ]

                    try:
                        results = await bot.send_media_group(
                            chat_id=chat_id,
                            media=_build_group("HTML"),
                            protect_content=protect_content,
                        )
                    except BadRequest as e:
                        if not _is_html_parse_error(e):
                            raise
                        logger.warning(f"HTML group caption failed for {chat_id}; retrying plain.")
                        results = await bot.send_media_group(
                            chat_id=chat_id,
                            media=_build_group(None),
                            protect_content=protect_content,
                        )
                    sent_ids.extend(m.message_id for m in results)
                    if cap_for_batch:
                        caption_attached = True

            # FIX: send overflow text (with keyboard) as separate message
            if text_overflow and text:
                try:
                    extra_ids = await _send_text_message(
                        bot, chat_id, text, protect_content, kb
                    )
                    sent_ids.extend(extra_ids)
                    kb_attached = bool(kb)
                except TelegramError as e:
                    logger.warning(f"Overflow text to {chat_id} failed: {e}")

            # FIX: send keyboard separately if still not attached
            # (isolated try so sent_ids are preserved even if button message fails)
            if kb and not kb_attached:
                try:
                    btn_msg = await bot.send_message(
                        chat_id=chat_id,
                        text="👇👇👇👇👇👇👇👇👇👇👇👇👇👇",
                        protect_content=protect_content,
                        reply_markup=kb,
                    )
                    sent_ids.append(btn_msg.message_id)
                except TelegramError as e:
                    logger.warning(f"Button message to {chat_id} failed: {e}")

    except TelegramError as e:
        logger.warning(f"send_configured_message to {chat_id} failed: {e}")
        # Keep any already-sent IDs so self-destruct is still scheduled

    if not sent_ids:
        return []

    # Schedule self-destruct for ALL successfully sent messages
    if destruct_seconds > 0:
        with get_session() as session:
            for mid in sent_ids:
                db.schedule_destruct(session, chat_id, mid, destruct_seconds)

    return sent_ids
