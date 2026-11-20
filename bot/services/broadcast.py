"""
Broadcast service — uses the unified message_sender.

Supports:
  • Users / Groups / All targets
  • Locked paywall teaser (zero content leak before payment)
  • Single-query purchase prefetch & batch deactivation on Forbidden
  • Resilient self-destruct via SelfDestructJob (retries transient network errors)
"""
import asyncio
import html
import json
import logging
from typing import Optional, Literal

from telegram import Bot, InlineKeyboardButton
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

from database.models import get_session, BroadcastMessage, MediaType, utcnow_naive
from database import db
from bot.services.message_sender import send_configured_message

logger = logging.getLogger(__name__)

BroadcastTarget = Literal["users", "groups", "all"]


def _pay_kb(message_id: int, stars: int) -> list[list[InlineKeyboardButton]]:
    return [[
        InlineKeyboardButton(
            f"🔓 فتح المحتوى — {stars} ⭐",
            callback_data=f"pay_{message_id}"
        )
    ]]


def _build_locked_teaser(title: Optional[str], stars: int, media_count: int = 0) -> str:
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
    raise_on_forbidden: bool = False,
) -> list[int]:
    is_locked_paid = (
        msg_data["is_paid"]
        and msg_data["stars_price"] > 0
        and not already_paid
        and user_db_id is not None
    )

    if is_locked_paid:
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
            raise_on_forbidden=raise_on_forbidden,
        )

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
        raise_on_forbidden=raise_on_forbidden,
    )


def _serialize_msg(msg: BroadcastMessage) -> dict:
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
        msg_data    = _serialize_msg(msg)
        eff_target  = target or msg_data["target"]
        users_data  = []
        groups_data = []
        purchased_ids: set[int] = set()

        if eff_target in ("users", "all"):
            users = db.get_all_active_users(session)
            users_data = [(u.telegram_id, u.id) for u in users]
            if msg_data["is_paid"]:
                purchased_ids = db.get_purchased_user_ids(session, msg_data["id"])
        if eff_target in ("groups", "all"):
            groups_data = db.get_active_groups(session)

    total   = len(users_data) + len(groups_data)
    success = failed = 0
    blocked_users: list[int] = []
    blocked_groups: list[int] = []

    # Users
    for tg_id, db_id in users_data:
        paid_ok = db_id in purchased_ids
        try:
            result = await _send_single(
                bot, tg_id, msg_data, db_id,
                already_paid=paid_ok, raise_on_forbidden=True,
            )
            if result:
                success += 1
            else:
                failed += 1
        except Forbidden:
            blocked_users.append(tg_id)
            failed += 1
        await asyncio.sleep(0.05)

    # Groups (unlocked delivery for groups)
    for g in groups_data:
        try:
            result = await _send_single(
                bot, g["chat_id"], msg_data, None,
                already_paid=True, raise_on_forbidden=True,
            )
            if result:
                success += 1
            else:
                failed += 1
        except Forbidden:
            blocked_groups.append(g["chat_id"])
            failed += 1
        await asyncio.sleep(0.05)

    is_repeating = bool(msg_data.get("interval_hours") and msg_data["interval_hours"] > 0)
    with get_session() as session:
        if blocked_users:
            db.deactivate_users_batch(session, blocked_users)
            logger.info(f"Deactivated {len(blocked_users)} users who blocked the bot.")
        if blocked_groups:
            db.deactivate_groups_batch(session, blocked_groups)
            logger.info(f"Deactivated {len(blocked_groups)} unreachable groups.")
        db.mark_broadcast_executed(session, message_id, is_repeating=is_repeating)

    return {
        "total":          total,
        "success":        success,
        "failed":         failed,
        "blocked_users":  len(blocked_users),
        "blocked_groups": len(blocked_groups),
        "users_total":    len(users_data),
        "groups_total":   len(groups_data),
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
    return bool(result)


# ─── Auto-delete expired messages (resilient) ────────────────────────────────

async def delete_expired_messages(bot: Bot):
    """Delete due messages; retry later on transient network/429 errors."""
    now = utcnow_naive()

    with get_session() as session:
        jobs = db.get_due_destruct_jobs(session, now)

    if not jobs:
        return

    done_ids: list[int] = []

    for job in jobs:
        try:
            await bot.delete_message(chat_id=job["chat_id"], message_id=job["message_id"])
            done_ids.append(job["id"])
            logger.debug(f"Auto-deleted msg {job['message_id']} in chat {job['chat_id']}")
        except RetryAfter as e:
            delay = min(float(e.retry_after), 15.0)
            logger.warning(f"Cleanup 429 RetryAfter ({delay}s); deferring remaining deletions.")
            await asyncio.sleep(delay)
            break
        except (TimedOut, NetworkError) as e:
            logger.warning(f"Transient delete error for job {job['id']}: {e}")
        except (BadRequest, Forbidden) as e:
            logger.debug(f"Permanent delete error for job {job['id']}: {e}")
            done_ids.append(job["id"])
        except TelegramError as e:
            logger.warning(f"Unexpected TelegramError deleting job {job['id']}: {e}")

        await asyncio.sleep(0.03)

    if done_ids:
        with get_session() as session:
            db.mark_destruct_done_batch(session, done_ids)
