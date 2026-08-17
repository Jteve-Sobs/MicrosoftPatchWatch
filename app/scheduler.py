import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.refresh_service import run_all_fetchers

logger = logging.getLogger("patchwatch.scheduler")
settings = get_settings()
scheduler = AsyncIOScheduler()


def start_scheduler() -> None:
    scheduler.add_job(
        run_all_fetchers,
        trigger=IntervalTrigger(hours=settings.fetch_interval_hours),
        kwargs={"trigger": "scheduler"},
        id="periodic-refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Scheduler started: checking all sources every %s hours", settings.fetch_interval_hours)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
