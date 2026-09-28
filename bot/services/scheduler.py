"""
APScheduler wrapper for scheduled broadcasts and cleanup jobs.
"""
import logging
import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from database.models import get_session, MessageStatus
from database import db

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started.")


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

    if interval_hours and interval_hours > 0:
        scheduler.add_job(
            _run_broadcast,
            trigger=IntervalTrigger(hours=interval_hours),
            id=job_id,
            args=[bot, message_id, target],
            next_run_time=run_at or datetime.datetime.utcnow(),
            replace_existing=True,
            misfire_grace_time=300,
        )
    else:
        run_time = run_at or datetime.datetime.utcnow()
        scheduler.add_job(
            _run_broadcast,
            trigger=DateTrigger(run_date=run_time),
            id=job_id,
            args=[bot, message_id, target],
            replace_existing=True,
            misfire_grace_time=300,
        )
    logger.info(f"Scheduled job {job_id} at {run_at} interval={interval_hours}h target={target}")


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
        trigger=IntervalTrigger(seconds=30),
        id="cleanup_expired",
        args=[bot],
        replace_existing=True,
    )
    logger.info("Cleanup job scheduled every 30s.")


# ─── Restore on startup ───────────────────────────────────────────────────────

def restore_scheduled_jobs(bot):
    with get_session() as session:
        pending = db.get_all_messages(session, status=MessageStatus.SCHEDULED)
        # Serialize inside session
        jobs = [
            {"id": m.id, "scheduled_at": m.scheduled_at, "interval_hours": m.interval_hours}
            for m in pending
        ]
    for j in jobs:
        schedule_broadcast(bot, j["id"], run_at=j["scheduled_at"], interval_hours=j["interval_hours"])
    logger.info(f"Restored {len(jobs)} scheduled broadcast jobs.")
