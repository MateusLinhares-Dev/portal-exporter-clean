from __future__ import annotations
import asyncio
from datetime import UTC, datetime, timedelta
import structlog
from softexpert_exporter.bootstrap import build_container
from softexpert_exporter.domain.models import SyncJob
from softexpert_exporter.infrastructure.logging import configure_logging

def retry_delay_seconds(job: SyncJob, base_seconds: int) -> int:
    return min(base_seconds * 2 ** max(job.attempts - 1, 0), 3600)

async def run() -> None:
    container = build_container()
    configure_logging(container.settings.LOG_LEVEL)
    logger = structlog.get_logger(__name__)
    logger.info('worker_started')
    try:
        while True:
            stale_before = datetime.now(UTC) - timedelta(seconds=container.settings.WORKER_STALE_AFTER_SECONDS)
            recovered = await container.repository.recover_stale_jobs(stale_before)
            if recovered:
                logger.warning('jobs_recovered', total=recovered)
            job = await container.repository.claim_next_job()
            if job is None:
                await asyncio.sleep(container.settings.WORKER_POLL_SECONDS)
                continue
            logger.info('job_started', job_id=job.id, mode=job.mode)
            try:
                result = await container.sync_use_case.execute(job)
                logger.info('job_done', job_id=job.id, status=result.status.value)
            except Exception as exc:
                summary = f'{type(exc).__name__}: {exc}'[:4000]
                if job.attempts < container.settings.WORKER_JOB_MAX_ATTEMPTS:
                    seconds = retry_delay_seconds(job, container.settings.WORKER_JOB_RETRY_SECONDS)
                    await container.repository.requeue_job(job.id, error_summary=summary,
                        next_attempt_at=datetime.now(UTC) + timedelta(seconds=seconds))
                    logger.exception('job_retry', job_id=job.id, seconds=seconds)
                else:
                    await container.repository.fail_job(job.id, summary)
                    logger.exception('job_failed', job_id=job.id)
    finally:
        await container.close()

if __name__ == '__main__':
    asyncio.run(run())
