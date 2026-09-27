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

async def _run_broadcast(bot, message_id: int):
    from bot.services.broadcast import broadcast
    logger.info(f"Running scheduled broadcast for message_id={message_id}")
    stats = await broadcast(bot, message_id)
    logger.info(f"Broadcast done: {stats}")


def schedule_broadcast(bot, message_id: int, run_at: datetime.datetime | None = None, interval_hours: float | None = None):
    """
    Schedule a broadcast.
    - run_at=None → run immediately (still via scheduler event loop)
    - interval_hours → repeating job
    """
    job_id = f"broadcast_{message_id}"

    if interval_hours and interval_hours > 0:
        scheduler.add_job(
            _run_broadcast,
            trigger=IntervalTrigger(hours=interval_hours),
            id=job_id,
            args=[bot, message_id],
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
            args=[bot, message_id],
            replace_existing=True,
            misfire_grace_time=300,
        )
    logger.info(f"Scheduled broadcast job {job_id} at {run_at} (interval={interval_hours}h)")


def cancel_broadcast(message_id: int):
    job_id = f"broadcast_{message_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info(f"Cancelled job {job_id}")


# ─── Cleanup job (delete expired messages) ───────────────────────────────────

async def _cleanup_job(bot):
    from bot.services.broadcast import delete_expired_messages
    await delete_expired_messages(bot)


def schedule_cleanup(bot):
    """Run every 30 seconds to delete time-expired messages."""
    scheduler.add_job(
        _cleanup_job,
        trigger=IntervalTrigger(seconds=30),
        id="cleanup_expired",
        args=[bot],
        replace_existing=True,
    )
    logger.info("Cleanup job scheduled every 30s.")


# ─── Restore pending scheduled jobs on startup ────────────────────────────────

def restore_scheduled_jobs(bot):
    """Restore SCHEDULED messages from DB on bot restart."""
    with get_session() as session:
        pending = db.get_all_messages(session, status=MessageStatus.SCHEDULED)
        for msg in pending:
            schedule_broadcast(bot, msg.id, run_at=msg.scheduled_at, interval_hours=msg.interval_hours)
    logger.info(f"Restored {len(pending)} scheduled broadcast jobs.")
