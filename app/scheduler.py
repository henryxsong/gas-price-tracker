import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
_scheduler = AsyncIOScheduler(timezone="UTC")

REFRESH_JOB_ID = "price_refresh"
NOTIFY_JOB_ID = "scheduled_notification"


async def _do_refresh() -> None:
    from database import AsyncSessionLocal
    from scrapers.manager import refresh_all
    from notifications import check_and_notify_thresholds, _get_settings, send_daily_summary

    async with AsyncSessionLocal() as db:
        statuses = await refresh_all(db)
        logger.info("Scheduled refresh: %s", statuses)
        settings = await _get_settings(db)
        if settings.get("notify_on_refresh") and settings.get("notifications_enabled"):
            await send_daily_summary(db)
        await check_and_notify_thresholds(db)


async def _do_notify() -> None:
    from database import AsyncSessionLocal
    from notifications import send_daily_summary

    async with AsyncSessionLocal() as db:
        await send_daily_summary(db)


def get_scheduler() -> AsyncIOScheduler:
    return _scheduler


def start() -> None:
    _scheduler.start()


def stop() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)


def update_refresh_job(interval_minutes: int, enabled: bool) -> None:
    try:
        _scheduler.remove_job(REFRESH_JOB_ID)
    except Exception:
        pass

    if enabled and interval_minutes > 0:
        _scheduler.add_job(
            _do_refresh,
            IntervalTrigger(minutes=interval_minutes),
            id=REFRESH_JOB_ID,
            replace_existing=True,
            misfire_grace_time=60,
        )
        logger.info("Refresh job scheduled every %d minutes", interval_minutes)


def update_notification_job(cron_expr: str, enabled: bool) -> None:
    try:
        _scheduler.remove_job(NOTIFY_JOB_ID)
    except Exception:
        pass

    if not enabled or not cron_expr.strip():
        return

    parts = cron_expr.strip().split()
    if len(parts) != 5:
        logger.warning("Invalid cron expression: %s", cron_expr)
        return

    minute, hour, day, month, day_of_week = parts
    _scheduler.add_job(
        _do_notify,
        CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
        ),
        id=NOTIFY_JOB_ID,
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info("Notification job scheduled: %s", cron_expr)
