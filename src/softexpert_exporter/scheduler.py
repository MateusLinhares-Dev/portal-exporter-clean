from __future__ import annotations
import asyncio
from datetime import datetime, UTC
from softexpert_exporter.date_windows import scheduled_semester
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from softexpert_exporter.bootstrap import build_container
from softexpert_exporter.infrastructure.logging import configure_logging

async def run() -> None:
    container = build_container()
    configure_logging(container.settings.LOG_LEVEL)
    logger = structlog.get_logger(__name__)
    if not container.settings.SCHEDULER_ENABLED:
        logger.info('scheduler_disabled')
        await container.close()
        return
    scheduler = AsyncIOScheduler(timezone=container.settings.SCHEDULER_TIMEZONE)
    async def enqueue() -> None:
        start, end = scheduled_semester(datetime.now(UTC), container.settings.SCHEDULER_TIMEZONE)
        options = {'window_start': start.isoformat(), 'window_end': end.isoformat(),
                   'schedule_key': f'semester:{start.isoformat()}:{end.isoformat()}'}
        job = await container.repository.create_job('scheduler', 'incremental', options)
        logger.info('scheduled_job_created', job_id=job.id)
    scheduler.add_job(enqueue, CronTrigger.from_crontab(container.settings.SCHEDULER_CRON,
        timezone=container.settings.SCHEDULER_TIMEZONE), id='portal-export',
        replace_existing=True, coalesce=True, max_instances=1)
    scheduler.start()
    logger.info('scheduler_started', cron=container.settings.SCHEDULER_CRON)
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)
        await container.close()

if __name__ == '__main__':
    asyncio.run(run())
