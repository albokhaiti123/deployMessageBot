"""
Unified message sender — sends any configured message with:
  • Single media or compliant media groups (max 10, grouped by compatible Telegram types)
  • Dedicated voice support (send_voice)
  • Caption > 1024 chars overflow handling
  • HTML parse-error fallback to plain text
  • Automatic RetryAfter (HTTP 429) backoff & Forbidden propagation option
  • Batched self-destruct scheduling (via SelfDestructJob)
"""
import asyncio
import logging
from typing import Optional

from telegram import (
    Bot, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument,
)
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from database.models import get_session
from database import db

logger = logging.getLogger(__name__)

MAX_MEDIA_GROUP = 10
MAX_CAPTION_LEN = 1024
MAX_TEXT_LEN    = 4096
MAX_429_RETRIES = 2
MAX_429_SLEEP   = 30.0


async def _call_with_retry(coro_factory):
    """Execute a Telegram API coroutine with automatic RetryAfter (429) handling."""
    for attempt in range(MAX_429_RETRIES + 1):
        try:
            return await coro_factory()
        except RetryAfter as e:
            if attempt >= MAX_429_RETRIES:
                raise
            delay = min(float(e.retry_after), MAX_429_SLEEP) + 0.5
            logger.warning(f"Telegram 429 RetryAfter: sleeping {delay:.1f}s (attempt {attempt + 1})")
            await asyncio.sleep(delay)


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
    if media_type in ("photo", "video"):
        return "visual"
    if media_type == "audio":
        return "audio"
    if media_type == "document":
        return "document"
    return "single_only"


def _partition_media_batches(media_list: list[dict]) -> list[list[dict]]:
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
    ids: list[int] = []
    chunks = [text[i:i + MAX_TEXT_LEN] for i in range(0, len(text), MAX_TEXT_LEN)] or ["—"]
    for idx, chunk in enumerate(chunks):
        chunk_kb = kb if idx == len(chunks) - 1 else None
        try:
            sent = await _call_with_retry(
                lambda c=chunk, r=chunk_kb: bot.send_message(
                    chat_id=chat_id, text=c, parse_mode="HTML",
                    protect_content=protect_content, reply_markup=r,
                )
            )
        except BadRequest as e:
            if not _is_html_parse_error(e):
                raise
            logger.warning(f"HTML parse failed for chat {chat_id}; falling back to plain text.")
            sent = await _call_with_retry(
                lambda c=chunk, r=chunk_kb: bot.send_message(
                    chat_id=chat_id, text=c, parse_mode=None,
                    protect_content=protect_content, reply_markup=r,
                )
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
    async def _dispatch(pm: Optional[str]):
        if media_type == "photo":
            return await _call_with_retry(lambda: bot.send_photo(
                chat_id=chat_id, photo=file_id, caption=caption,
                parse_mode=pm, has_spoiler=spoiler,
                protect_content=protect, reply_markup=kb,
            ))
        if media_type == "video":
            return await _call_with_retry(lambda: bot.send_video(
                chat_id=chat_id, video=file_id, caption=caption,
                parse_mode=pm, has_spoiler=spoiler,
                protect_content=protect, reply_markup=kb,
            ))
        if media_type == "audio":
            return await _call_with_retry(lambda: bot.send_audio(
                chat_id=chat_id, audio=file_id, caption=caption,
                parse_mode=pm, protect_content=protect, reply_markup=kb,
            ))
        if media_type == "voice":
            return await _call_with_retry(lambda: bot.send_voice(
                chat_id=chat_id, voice=file_id, caption=caption,
                parse_mode=pm, protect_content=protect, reply_markup=kb,
            ))
        if media_type == "document":
            return await _call_with_retry(lambda: bot.send_document(
                chat_id=chat_id, document=file_id, caption=caption,
                parse_mode=pm, protect_content=protect, reply_markup=kb,
            ))
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
    raise_on_forbidden: bool = False,
) -> list[int]:
    """
    Send a message and optionally schedule self-destruct.
    Returns list of ALL Telegram message_ids sent, or empty list on failure.
    """
    kb       = _build_keyboard(buttons or [], extra_buttons)
    sent_ids: list[int] = []

    effective_media: list[dict] = []
    if media_list:
        effective_media = [m for m in media_list if m.get("file_id")]
    elif media_type and media_type != "none" and media_file_id:
        effective_media = [{"type": media_type, "file_id": media_file_id}]

    text_overflow = bool(effective_media and text and len(text) > MAX_CAPTION_LEN)
    inline_caption = None if text_overflow else text

    try:
        if not effective_media:
            sent_ids.extend(await _send_text_message(
                bot, chat_id, text or "—", protect_content, kb
            ))
        else:
            batches = _partition_media_batches(effective_media)
            caption_attached = False
            kb_attached = False

            for batch_idx, batch in enumerate(batches):
                is_last_batch = (batch_idx == len(batches) - 1)
                cap_for_batch = inline_caption if (not caption_attached and inline_caption) else None

                if len(batch) == 1:
                    item = batch[0]
                    attach_kb = kb if (is_last_batch and not text_overflow) else None
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
                else:
                    def _build_group(pm: Optional[str]):
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
                        results = await _call_with_retry(
                            lambda: bot.send_media_group(
                                chat_id=chat_id,
                                media=_build_group("HTML"),
                                protect_content=protect_content,
                            )
                        )
                    except BadRequest as e:
                        if not _is_html_parse_error(e):
                            raise
                        logger.warning(f"HTML media_group caption failed for {chat_id}; retrying plain text.")
                        results = await _call_with_retry(
                            lambda: bot.send_media_group(
                                chat_id=chat_id,
                                media=_build_group(None),
                                protect_content=protect_content,
                            )
                        )
                    sent_ids.extend(m.message_id for m in results)
                    if cap_for_batch:
                        caption_attached = True

            if text_overflow and text:
                try:
                    sent_ids.extend(await _send_text_message(
                        bot, chat_id, text, protect_content, kb
                    ))
                    kb_attached = bool(kb)
                except Forbidden:
                    if raise_on_forbidden and not sent_ids:
                        raise
                except TelegramError as e:
                    logger.warning(f"Overflow text send to {chat_id} failed: {e}")

            if kb and not kb_attached:
                try:
                    btn_msg = await _call_with_retry(
                        lambda: bot.send_message(
                            chat_id=chat_id,
                            text="👇👇👇👇👇👇👇👇👇👇👇👇👇👇",
                            protect_content=protect_content,
                            reply_markup=kb,
                        )
                    )
                    sent_ids.append(btn_msg.message_id)
                except Forbidden:
                    if raise_on_forbidden and not sent_ids:
                        raise
                except TelegramError as e:
                    logger.warning(f"Separate button message to {chat_id} failed: {e}")

    except Forbidden:
        if raise_on_forbidden and not sent_ids:
            raise
        logger.info(f"Chat {chat_id} blocked/kicked the bot.")
    except TelegramError as e:
        logger.warning(f"send_configured_message to {chat_id} failed: {e}")

    if not sent_ids:
        return []

    if destruct_seconds > 0:
        with get_session() as session:
            db.schedule_destruct_batch(session, chat_id, sent_ids, destruct_seconds)

    return sent_ids
