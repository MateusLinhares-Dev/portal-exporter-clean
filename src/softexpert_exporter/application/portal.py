from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import shutil
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import structlog
from playwright.async_api import BrowserContext, Error as PlaywrightError
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

    async def _download_pdf_in_browser(self, url: str, context: BrowserContext, html: bytes) -> bytes:
        """Try a browser-side report export when an authenticated GET returns HTML.

        Captures actual PDF network responses and browser downloads, without dumping
        HTML, request cookies, query strings or credentials into application logs.
        """
        if not allowed_report_url(url, self.settings.report_origins):
            raise ValueError(f'Browser download origin not allowed: {origin(url)}')

        events: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        child_pages = []
        browser_downloads = []
        existing = set(context.pages)
        page = None

        def handle_response(response) -> None:
            if response.status != 200 or not allowed_report_url(
                response.url, self.settings.report_origins
            ):
                return
            ct = response.headers.get('content-type', '').lower()
            cd = response.headers.get('content-disposition', '').lower()
            if ('application/pdf' in ct or
                    'application/octet-stream' in ct or
                    ('attachment' in cd and '.pdf' in cd)):
                events.put_nowait(('response', response))

        def handle_download(download) -> None:
            browser_downloads.append(download)
            if allowed_report_url(download.url, self.settings.report_origins):
                events.put_nowait(('download', download))

        def handle_page(new_page) -> None:
            if new_page not in existing:
                child_pages.append(new_page)
                new_page.on('download', handle_download)

        async def collect_pdf(seconds: float) -> bytes | None:
            deadline = asyncio.get_running_loop().time() + seconds
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                try:
                    kind, obj = await asyncio.wait_for(events.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None
                try:
                    if kind == 'response':
                        size = int(obj.headers.get('content-length') or '0')
                        if size > self.settings.PORTAL_MAX_PDF_BYTES:
                            raise ValueError('Browser PDF exceeds size limit')
                        raw = await asyncio.wait_for(obj.body(), timeout=90)
                    else:
                        path = Path(await asyncio.wait_for(obj.path(), timeout=90))
                        if path.stat().st_size > self.settings.PORTAL_MAX_PDF_BYTES:
                            raise ValueError('Browser PDF exceeds size limit')
                        try:
                            raw = await asyncio.to_thread(path.read_bytes)
                        finally:
                            await obj.delete()
                    if len(raw) > self.settings.PORTAL_MAX_PDF_BYTES:
                        raise ValueError('Browser PDF exceeds size limit')
                    if raw.lstrip(b'\xef\xbb\xbf\r\n ').startswith(b'%PDF-'):
                        logger.info('pdf_browser_fallback_ok', path=urlsplit(url).path,
                                    bytes=len(raw))
                        return raw
                except (PlaywrightError, OSError, asyncio.TimeoutError) as exc:
                    logger.warning('pdf_browser_capture_failed',
                                   reason=type(exc).__name__)
                # A candidate that is not a PDF is ignored; await a genuine PDF.

        context.on('response', handle_response)
        context.on('page', handle_page)
        try:
            page = await context.new_page()
            page.on('download', handle_download)
            try:
                await page.goto(url, referer=self.settings.PORTAL_ENTRY_URL,
                                wait_until='domcontentloaded', timeout=30000)
            except PlaywrightError as exc:
                # Chromium can raise a navigation error for downloads; the
                # corresponding download event is still captured above.
                logger.debug('pdf_browser_navigation', reason=type(exc).__name__)

            pdf = await collect_pdf(8)
            if pdf is not None:
                return pdf

            # Some HTML report viewers only generate the file after a click.
            # Click in the isolated viewer page, never in the crawler's tab.
            if page and not page.is_closed():
                trigger = page.get_by_role('button', name=re.compile(
                    r'(gerar|baixar|download|exportar).{0,25}(pdf|arquivo)|^(baixar|download)$', re.I))
                try:
                    if await trigger.count():
                        await trigger.first.click(timeout=5000)
                    else:
                        links = page.get_by_role('link', name=re.compile(
                            r'(gerar|baixar|download|exportar).{0,25}(pdf|arquivo)|^(baixar|download)$', re.I))
                        if await links.count():
                            await links.first.click(timeout=5000)
                except PlaywrightError:
                    pass
            pdf = await collect_pdf(12)
            if pdf is not None:
                return pdf

            # Keep only non-sensitive diagnostic metadata. Do not log URLs
            # with query parameters, HTML bodies or session identifiers.
            title_match = re.search(rb'<title[^>]*>(.*?)</title>', html[:120000], re.I | re.S)
            title = (re.sub(rb'\s+', b' ', title_match.group(1))[:100].decode(
                'utf-8', errors='replace') if title_match else 'unknown')
            login_form = bool(re.search(rb'type\s*=\s*[\"\']password', html, re.I))
            raise RuntimeError(
                f'Report returned HTML, not PDF; html_title={title!r}; '
                f'login_form={login_form}; path={urlsplit(url).path}; '
                'browser fallback captured no PDF. Inspect the report link/action '
                'in the authenticated portal.'
            )
        finally:
            context.remove_listener('response', handle_response)
            context.remove_listener('page', handle_page)
            for child in child_pages:
                try:
                    if not child.is_closed():
                        await child.close()
                except PlaywrightError:
                    pass
            if page is not None:
                try:
                    if not page.is_closed():
                        await page.close()
                except PlaywrightError:
                    pass
            for download in browser_downloads:
                try:
                    await asyncio.wait_for(download.delete(), timeout=10)
                except (PlaywrightError, OSError, asyncio.TimeoutError):
                    pass

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
                    logger.warning('pdf_html_fallback', origin=origin(current),
                                   path=urlsplit(current).path, bytes=len(data))
                    return await self._download_pdf_in_browser(current, context, data)
                return data
            finally:
                await response.dispose()
        raise RuntimeError('Too many redirects')

    async def execute(self, job: SyncJob) -> SyncJob:
        dry_run = job.mode == 'test' and job.options.get('dry_run', False)
        if not dry_run and not await self.storage.is_ready():
            raise OSError('Output share not mounted/writable')
        crawler = PortalCrawler(self.settings)
        tmp_dir = self.settings.TEMP_DIR / job.id
        await asyncio.to_thread(tmp_dir.mkdir, parents=True, exist_ok=True)
        count = dict(total=0, downloaded=0, skipped=0, failed=0, out_of_period=0,
                     missing_date=0, validated=0)
        errors: list[str] = []

        async def on_node(node, status: str, message: str) -> None:
            await self.repository.audit_event(
                job_id=job.id, attempt=job.attempts, kind='navigation', status=status,
                tab=node.tab, portal_path=' / '.join(node.trail), message=message)

        async def on_report(report: ReportDescriptor, context: BrowserContext) -> None:
            count['total'] += 1
            await self.repository.heartbeat(job.id)
            included, reason = eligible_report(job.mode, report.date, job.options)
            if not included:
                if reason == 'missing_or_invalid_date':
                    count['missing_date'] += 1
                    count['failed'] += 1
                    message = f'{report.document_id}: data ausente ou invalida; nao exportado'
                    errors.append(message)
                    await self.repository.audit_event(job_id=job.id,attempt=job.attempts,
                        kind='document', status='missing_date', descriptor=report,message=message)
                else:
                    count['out_of_period'] += 1
                    await self.repository.audit_event(job_id=job.id,attempt=job.attempts,
                        kind='document', status='out_of_period', descriptor=report,
                        message='Fora da janela de datas aprovada para esta execucao')
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
                if not dry_run and not job.options.get('force', False):
                    stored = await self.repository.extracted_path(source_key)
                    if stored and await self.storage.exists(stored):
                        count['skipped'] += 1
                        await self.repository.audit_event(job_id=job.id, attempt=job.attempts,
                            kind='document',status='skipped',descriptor=report,
                            file_name=stored.rsplit('/',1)[-1],destination_path=stored,
                            message='Documento ja existe no destino; download nao repetido')
                        return
                if shutil.disk_usage(tmp_dir).free < 2 * self.settings.PORTAL_MAX_PDF_BYTES:
                    raise OSError('Temporary filesystem has insufficient free space')
                pdf = await self._download_pdf(report.url, context)
                if dry_run:
                    count['validated'] += 1
                    await self.repository.audit_event(job_id=job.id, attempt=job.attempts,
                        kind='document', status='validated', descriptor=report,
                        file_name=name, destination_path=relative_path,
                        size_bytes=len(pdf), checksum_sha256=hashlib.sha256(pdf).hexdigest(),
                        message='PDF validado sem gravação; modo de teste de carga')
                    return
                await asyncio.to_thread(temp.write_bytes, pdf)
                checksum = hashlib.sha256(pdf).hexdigest()
                target = await self.storage.store(temp, relative_path, checksum, len(pdf))
                await self.repository.mark_extracted(job_id=job.id, source_key=source_key,
                    descriptor=report, destination_path=target.relative_path,
                    checksum_sha256=checksum, size_bytes=len(pdf))
                count['downloaded'] += 1
                await self.repository.audit_event(job_id=job.id, attempt=job.attempts,
                    kind='document',status='downloaded',descriptor=report,
                    file_name=target.relative_path.rsplit('/',1)[-1],
                    destination_path=target.relative_path,
                    size_bytes=len(pdf),checksum_sha256=checksum)
                logger.info('pdf_stored', job_id=job.id, document=report.document_id,
                            destination=target.relative_path)
            except Exception as exc:
                count['failed'] += 1
                message = f'{report.document_id}: {type(exc).__name__}: {exc}'[:600]
                errors.append(message)
                if not dry_run:
                    await self.repository.mark_extraction_failed(job_id=job.id,
                        source_key=source_key, descriptor=report, error_message=message)
                await self.repository.audit_event(job_id=job.id, attempt=job.attempts,
                    kind='document',status='failed',descriptor=report,
                    file_name=name,destination_path=relative_path,message=message)
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
                lambda: self.repository.heartbeat(job.id), on_node=on_node)
            if discovered == 0 and crawler.issues:
                raise RuntimeError(
                    'No PDF links discovered and crawler reported navigation errors: '
                    + ' | '.join(crawler.issues[:3])[:700]
                )
            if discovered == 0:
                raise RuntimeError('Crawler finished without discovering any PDF reports; '
                                   'the job cannot be marked as successful')
            errors.extend(crawler.issues)
            count['failed'] += len(crawler.issues)
            summary_parts = [f"out_of_period={count['out_of_period']}",
                             f"missing_date={count['missing_date']}",
                             f"dry_run={dry_run}", f"validated={count['validated']}"]
            summary_parts.extend(errors[:8])
            summary = ' | '.join(summary_parts)[:4000]
            return await self.repository.complete_job(job.id, total_records=count['total'],
                downloaded=count['downloaded'], skipped=count['skipped'],
                failed=count['failed'], error_summary=summary, validated=count['validated'])
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            try:
                tmp_dir.rmdir()
            except OSError:
                pass
