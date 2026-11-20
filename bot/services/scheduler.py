"""
APScheduler wrapper for scheduled broadcasts and cleanup jobs.

Phase-1 fixes:
  - Scheduler timezone: uses config.SCHEDULER_TIMEZONE (not hardcoded "UTC")
  - restore_scheduled_jobs:
      * reads `target` from DB
      * computes correct next_run_time for repeating jobs (no immediate re-fire)
      * marks overdue one-shot jobs as CANCELLED instead of leaving them SCHEDULED
  - schedule_broadcast: passes timezone-aware run_at to APScheduler
"""
import logging
import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
from database.models import get_session, MessageStatus
from database import db

logger = logging.getLogger(__name__)

MISFIRE_GRACE_SECONDS = 300


def get_scheduler_tz() -> ZoneInfo:
    """Return configured timezone, falling back to UTC on invalid value."""
    try:
        return ZoneInfo(config.SCHEDULER_TIMEZONE)
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning(
            f"Invalid SCHEDULER_TIMEZONE '{config.SCHEDULER_TIMEZONE}', falling back to UTC."
        )
        return ZoneInfo("UTC")


def _utc_aware(dt: datetime.datetime) -> datetime.datetime:
    """Ensure a datetime is UTC-aware (naive datetimes from DB are assumed UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


# FIX: use config timezone instead of hardcoded "UTC"
scheduler = AsyncIOScheduler(timezone=get_scheduler_tz())


def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        logger.info(f"Scheduler started (timezone={get_scheduler_tz()}).")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


# ─── Broadcast job ────────────────────────────────────────────────────────────

async def _run_broadcast(bot, message_id: int, target: str = "users"):
    from bot.services.broadcast import broadcast
    logger.info(f"Running scheduled broadcast msg={message_id} target={target}")
    stats = await broadcast(bot, message_id, target=target)
    logger.info(f"Broadcast done: {stats}")


def schedule_broadcast(
    bot,
    message_id: int,
    run_at: datetime.datetime | None = None,
    interval_hours: float | None = None,
    target: str = "users",
):
    """Schedule a broadcast. Supports one-shot and repeating."""
    job_id = f"broadcast_{message_id}"
    tz = get_scheduler_tz()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    run_time = _utc_aware(run_at) if run_at else now_utc

    if interval_hours and interval_hours > 0:
        scheduler.add_job(
            _run_broadcast,
            trigger=IntervalTrigger(hours=interval_hours, timezone=tz),
            id=job_id,
            args=[bot, message_id, target],
            next_run_time=run_time,
            replace_existing=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )
    else:
        scheduler.add_job(
            _run_broadcast,
            trigger=DateTrigger(run_date=run_time, timezone=tz),
            id=job_id,
            args=[bot, message_id, target],
            replace_existing=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )
    logger.info(
        f"Scheduled job {job_id} next_run={run_time.astimezone(tz).isoformat()} "
        f"interval={interval_hours}h target={target}"
    )


def cancel_broadcast(message_id: int):
    job_id = f"broadcast_{message_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info(f"Cancelled job {job_id}")


# ─── Cleanup job ─────────────────────────────────────────────────────────────

async def _cleanup_job(bot):
    from bot.services.broadcast import delete_expired_messages
    await delete_expired_messages(bot)


def schedule_cleanup(bot):
    scheduler.add_job(
        _cleanup_job,
        trigger=IntervalTrigger(seconds=15, timezone=get_scheduler_tz()),
        id="cleanup_expired",
        args=[bot],
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("Cleanup job scheduled every 15s.")


async def _purge_job():
    with get_session() as session:
        removed = db.purge_old_destruct_jobs(session, older_than_days=7)
    if removed:
        logger.info(f"Purged {removed} old SelfDestructJob records.")


def schedule_purge():
    scheduler.add_job(
        _purge_job,
        trigger=IntervalTrigger(hours=24, timezone=get_scheduler_tz()),
        id="purge_old_destruct_jobs",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("Daily SelfDestructJob purge scheduled.")


# ─── Restore on startup ───────────────────────────────────────────────────────

def restore_scheduled_jobs(bot):
    """
    FIX:
      - reads target from DB
      - computes next_run_time for repeating jobs (avoids immediate re-fire)
      - marks overdue one-shot jobs as CANCELLED
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    with get_session() as session:
        pending = db.get_all_messages(session, status=MessageStatus.SCHEDULED)
        jobs = [
            {
                "id":             m.id,
                "scheduled_at":   m.scheduled_at,
                "interval_hours": m.interval_hours,
                "sent_at":        m.sent_at,
                "target":         getattr(m, "target", "users") or "users",
            }
            for m in pending
        ]

    restored = 0
    for j in jobs:
        if j["interval_hours"] and j["interval_hours"] > 0:
            # FIX: compute next run from last sent_at to avoid immediate re-fire
            step = datetime.timedelta(hours=j["interval_hours"])
            if j["sent_at"]:
                next_run = _utc_aware(j["sent_at"]) + step
                # Advance until future
                while next_run <= now_utc:
                    next_run += step
            elif j["scheduled_at"] and _utc_aware(j["scheduled_at"]) > now_utc:
                next_run = _utc_aware(j["scheduled_at"])
            else:
                # No reference → schedule 10s from now (safe, not immediate)
                next_run = now_utc + datetime.timedelta(seconds=10)

            schedule_broadcast(
                bot, j["id"],
                run_at=next_run,
                interval_hours=j["interval_hours"],
                target=j["target"],
            )
            restored += 1

        else:
            # One-shot
            if not j["scheduled_at"]:
                continue
            run_utc = _utc_aware(j["scheduled_at"])
            overdue_secs = (now_utc - run_utc).total_seconds()

            # FIX: if missed grace window → cancel silently
            if overdue_secs > MISFIRE_GRACE_SECONDS:
                logger.warning(
                    f"One-shot broadcast #{j['id']} missed its time ({run_utc}); marking CANCELLED."
                )
                with get_session() as session:
                    db.update_message_status(session, j["id"], MessageStatus.CANCELLED)
                continue

            schedule_broadcast(
                bot, j["id"],
                run_at=run_utc,
                interval_hours=None,
                target=j["target"],
            )
            restored += 1

    logger.info(f"Restored {restored} scheduled broadcast jobs.")
