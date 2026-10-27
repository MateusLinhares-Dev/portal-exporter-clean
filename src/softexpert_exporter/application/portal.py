from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
from urllib.parse import urljoin, urlsplit
import re
from html import unescape

import structlog
from playwright.async_api import BrowserContext, TimeoutError as PlaywrightTimeoutError
from softexpert_exporter.config import Settings
from softexpert_exporter.date_windows import eligible_report
from softexpert_exporter.domain.models import ReportDescriptor, SyncJob
from softexpert_exporter.domain.ports import FileStorage, JobRepository
from softexpert_exporter.infrastructure.portal_scraper import (
    PortalCrawler, allowed_report_url, origin, source_identity,
)
from softexpert_exporter.infrastructure.storage import sanitize_component

logger = structlog.get_logger(__name__)

class PortalScrapeUseCase:
    def __init__(self, *, settings: Settings, storage: FileStorage, repository: JobRepository):
        self.settings = settings
        self.storage = storage
        self.repository = repository

    async def _download_pdf_in_browser(
        self,
        url: str,
        context: BrowserContext
    ) -> bytes:
        page = await context.new_page()

        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000
            )

            if response is None or response.status != 200:
                raise RuntimeError("Não foi possível abrir o visualizador")

            deadline = asyncio.get_running_loop().time() + 15
            visited: set[str] = set()

            while asyncio.get_running_loop().time() < deadline:
                for frame in page.frames:
                    try:
                        html = await frame.content()
                    except Exception:
                        continue

                    links = re.findall(
                        r"""["']([^"']+?\.pdf(?:\?[^"']*)?)["']""",
                        html,
                        flags=re.IGNORECASE
                    )

                    for link in links:
                        link = unescape(link.replace("\\/", "/"))
                        base = frame.url if frame.url.startswith("http") else page.url
                        pdf_url = urljoin(base, link)

                        if pdf_url in visited:
                            continue

                        visited.add(pdf_url)

                        if not urlsplit(pdf_url).path.lower().endswith(".pdf"):
                            continue

                        if not allowed_report_url(
                            pdf_url,
                            self.settings.report_origins
                        ):
                            continue

                        return await self._download_pdf(pdf_url, context)

                await asyncio.sleep(0.5)

            raise RuntimeError(
                "Nenhuma URL .pdf autorizada foi encontrada no HTML do visualizador"
            )

        finally:
            await page.close()

    async def _download_pdf(self, url: str, context: BrowserContext) -> bytes:
        current = url
        for _ in range(5):
            if not allowed_report_url(current, self.settings.report_origins):
                raise ValueError(f'Download destination not allowed: {origin(current)}')
            response = await context.request.get(
                current, max_redirects=0, timeout=90000,
                headers={'Referer': self.settings.PORTAL_ENTRY_URL,
                         'Accept': 'application/pdf,application/octet-stream,*/*'},
            )
            try:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get('location')
                    if not location:
                        raise RuntimeError('Redirect without Location header')
                    current = urljoin(current, location)
                    continue

                if response.status != 200:
                    raise RuntimeError(
                        f'Report server returned HTTP {response.status}; '
                        f'content_type={response.headers.get("content-type", "unknown")}; '
                        f'host={origin(current)}'
                    )
                length = int(response.headers.get('content-length') or '0')
                if length > self.settings.PORTAL_MAX_PDF_BYTES:
                    raise ValueError('PDF is larger than PORTAL_MAX_PDF_BYTES')
                data = await response.body()
                if len(data) > self.settings.PORTAL_MAX_PDF_BYTES:
                    raise ValueError('PDF exceeded maximum allowed size')
                if not data.lstrip(b'\xef\xbb\xbf\r\n ').startswith(b'%PDF-'):
                    return await self._download_pdf_in_browser(current, context)
                return data
            finally:
                await response.dispose()
        raise RuntimeError('Too many redirects')

    async def execute(self, job: SyncJob) -> SyncJob:
        if not await self.storage.is_ready():
            raise OSError('Output share not mounted/writable')
        crawler = PortalCrawler(self.settings)
        tmp_dir = self.settings.TEMP_DIR / job.id
        await asyncio.to_thread(tmp_dir.mkdir, parents=True, exist_ok=True)
        count = dict(total=0, downloaded=0, skipped=0, failed=0, out_of_period=0, missing_date=0)
        errors: list[str] = []

        async def on_report(report: ReportDescriptor, context: BrowserContext) -> None:
            count['total'] += 1
            await self.repository.heartbeat(job.id)
            included, reason = eligible_report(job.mode, report.date, job.options)
            if not included:
                if reason == 'missing_or_invalid_date':
                    count['missing_date'] += 1
                    count['failed'] += 1
                    errors.append(f'{report.document_id}: missing/unparseable report date; not exported')
                else:
                    count['out_of_period'] += 1
                return
            url = source_identity(report.url)
            source_key = hashlib.sha256(url.encode('utf-8')).hexdigest()
            file_digest = source_key[:12]
            name = sanitize_component(report.code or report.document_id) + f'-{file_digest}.pdf'
            parts = [sanitize_component(p, 'Portal') for p in report.portal_path]
            relative_path = '/'.join([*parts, sanitize_component(report.tab, 'Geral'),
                                      sanitize_component(report.unit, 'Sem unidade'), name])
            temp = tmp_dir / f'{file_digest}.download'
            try:
                if not job.options.get('force', False):
                    stored = await self.repository.extracted_path(source_key)
                    if stored and await self.storage.exists(stored):
                        count['skipped'] += 1
                        return
                pdf = await self._download_pdf(report.url, context)
                await asyncio.to_thread(temp.write_bytes, pdf)
                checksum = hashlib.sha256(pdf).hexdigest()
                target = await self.storage.store(temp, relative_path, checksum, len(pdf))
                await self.repository.mark_extracted(job_id=job.id, source_key=source_key,
                    descriptor=report, destination_path=target.relative_path,
                    checksum_sha256=checksum, size_bytes=len(pdf))
                count['downloaded'] += 1
                logger.info('pdf_stored', job_id=job.id, document=report.document_id,
                            destination=target.relative_path)
            except Exception as exc:
                count['failed'] += 1
                message = f'{report.document_id}: {type(exc).__name__}: {exc}'[:600]
                errors.append(message)
                await self.repository.mark_extraction_failed(job_id=job.id,
                    source_key=source_key, descriptor=report, error_message=message)
                logger.warning('pdf_failed', job_id=job.id, document=report.document_id,
                               reason=type(exc).__name__, detail=str(exc)[:350],
                               report_origin=origin(report.url))
                if isinstance(exc, OSError):
                    raise
            finally:
                temp.unlink(missing_ok=True)

        async def periodic_heartbeat() -> None:
            while True:
                await asyncio.sleep(25)
                try:
                    await self.repository.heartbeat(job.id)
                except Exception:
                    logger.exception('job_heartbeat_failed', job_id=job.id)

        heartbeat_task = asyncio.create_task(periodic_heartbeat())
        try:
            discovered = await crawler.crawl(job, on_report,
                                              lambda: self.repository.heartbeat(job.id))
            if discovered == 0 and crawler.issues:
                raise RuntimeError(
                    'No PDF links discovered and crawler reported navigation errors: '
                    + ' | '.join(crawler.issues[:3])[:700]
                )
            if discovered == 0:
                logger.info('crawl_completed_without_reports', job_id=job.id)
            errors.extend(crawler.issues)
            count['failed'] += len(crawler.issues)
            summary_parts = [f"out_of_period={count['out_of_period']}",
                             f"missing_date={count['missing_date']}"]
            summary_parts.extend(errors[:8])
            summary = ' | '.join(summary_parts)[:4000]
            return await self.repository.complete_job(job.id, total_records=count['total'],
                downloaded=count['downloaded'], skipped=count['skipped'],
                failed=count['failed'], error_summary=summary)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            try:
                tmp_dir.rmdir()
            except OSError:
                pass
