import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
_scheduler = AsyncIOScheduler(timezone="UTC")


def refresh_job_id(user_id: int) -> str:
    return f"refresh_user_{user_id}"


def notify_job_id(user_id: int) -> str:
    return f"notify_user_{user_id}"


async def _do_refresh_user(user_id: int) -> None:
    from database import AsyncSessionLocal
    from models import User
    from sqlalchemy import select
    from scrapers.manager import refresh_all
    from notifications import check_and_notify_thresholds, send_daily_summary

    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not user:
            return

        statuses = await refresh_all(db, user_id=user_id)
        logger.info("Scheduled refresh for user %d: %s", user_id, statuses)

        import json
        await db.refresh(user, ["setting"])
        settings = json.loads(user.setting.value) if user.setting else {}

        if settings.get("notify_on_refresh") and settings.get("notifications_enabled"):
            await send_daily_summary(db, user)
        await check_and_notify_thresholds(db, user)


async def _do_notify_user(user_id: int) -> None:
    from database import AsyncSessionLocal
    from models import User
    from sqlalchemy import select
    from notifications import send_daily_summary

    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user:
            await send_daily_summary(db, user)


def get_scheduler() -> AsyncIOScheduler:
    return _scheduler


def start() -> None:
    _scheduler.start()


def stop() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)


def update_user_refresh_job(user_id: int, interval_minutes: int, enabled: bool) -> None:
    job_id = refresh_job_id(user_id)
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass

    if enabled and interval_minutes > 0:
        _scheduler.add_job(
            _do_refresh_user,
            IntervalTrigger(minutes=interval_minutes),
            args=[user_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=60,
        )
        logger.info("Refresh job scheduled for user %d every %d minutes", user_id, interval_minutes)


def update_user_notification_job(user_id: int, cron_expr: str, enabled: bool) -> None:
    job_id = notify_job_id(user_id)
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass

    if not enabled or not cron_expr.strip():
        return

    parts = cron_expr.strip().split()
    if len(parts) != 5:
        logger.warning("Invalid cron expression for user %d: %s", user_id, cron_expr)
        return

    minute, hour, day, month, day_of_week = parts
    _scheduler.add_job(
        _do_notify_user,
        CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week),
        args=[user_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info("Notification job scheduled for user %d: %s", user_id, cron_expr)
