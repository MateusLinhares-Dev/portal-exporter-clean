from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit, urlunsplit

import structlog
from playwright.async_api import BrowserContext, Page, async_playwright, TimeoutError as PlaywrightTimeoutError

from softexpert_exporter.domain.models import ReportDescriptor, SyncJob

logger = structlog.get_logger(__name__)

TAB_SELECTOR = '[class*="FolderTabItemContent_tabContainer"], [role="tab"]'

PORTAL_SELECTOR = '.ActionInitializerItem, [class*="ActionInitializerItem"]'
MORE_TEXT = re.compile(r'carregar mais registros|clique aqui para carregar mais|load more', re.I)

def origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Expected an absolute HTTP(S) URL')
    if parsed.username or parsed.password or '\\' in url:
        raise ValueError('Invalid URL')
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('Invalid TCP port') from exc
    host = parsed.hostname.lower()
    return f'{parsed.scheme}://{host}{":" + str(port) if port else ""}'

def allowed_report_url(url: str, allowlist: set[str]) -> bool:
    try:
        return origin(url) in allowlist
    except ValueError:
        return False

def source_identity(url: str) -> str:
    return urlunsplit(urlsplit(url)._replace(fragment=''))

def record_identifier(url: str) -> str:
    query = parse_qs(urlsplit(url).query)
    for key in ('identificador', 'workflowId', 'id', 'cddocument'):
        value = query.get(key)
        if value:
            return value[0][:255]
    return urlsplit(url).path.rsplit('/', 1)[-1][:255] or 'relatorio'

@dataclass(frozen=True, slots=True)
class Action:
    kind: str
    label: str
    occurrence: int = 0

@dataclass(frozen=True, slots=True)
class Node:
    actions: tuple[Action, ...] = ()
    trail: tuple[str, ...] = ('Checklist - Metrô',)
    depth: int = 0
    tab: str = 'Geral'

class PortalCrawler:
    def __init__(self, settings):
        self.settings = settings
        self.base_origin = settings.portal_origin
        self.allowlist = settings.report_origins
        self.issues: list[str] = []
        self.portals_seen = 0
        self.reports_seen = 0

    async def _portal_is_ready(self, page: Page) -> bool:
        for frame in page.frames:
            try:
                tabs = frame.locator(TAB_SELECTOR)
                if await tabs.count() and await tabs.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _wait_tab_dom(self, page: Page) -> None:
        deadline = asyncio.get_running_loop().time() + self.settings.PORTAL_TIMEOUT_MS / 1000
        while asyncio.get_running_loop().time() < deadline:
            if await self._portal_is_ready(page):
                return
            await asyncio.sleep(0.3)
        raise RuntimeError(
            'Dashboard tab components did not render; '
            f'frame_count={len(page.frames)}, path={urlsplit(page.url).path}'
        )

    async def _settle_after_navigation(self, page: Page) -> None:
        await page.wait_for_timeout(900)

        await self._scroll_dashboards(page)

        stable = 0
        previous = None
        deadline = asyncio.get_running_loop().time() + min(8, self.settings.PORTAL_TIMEOUT_MS / 1000)

        while asyncio.get_running_loop().time() < deadline:
            state = []
            for frame in page.frames:
                try:
                    row_count = await frame.locator('tr').count()
                    tile_count = await frame.locator(PORTAL_SELECTOR).count()
                    report_count = await frame.locator('a[href*="relatorio_"]').count()
                    state.append((frame.url, row_count, tile_count, report_count))
                except Exception:
                    continue
            state = tuple(state)
            stable = stable + 1 if state == previous else 0
            if stable >= 3:
                return
            previous = state
            await page.wait_for_timeout(450)

    async def _wait_root_state(self, page: Page, *, allow_login: bool) -> str:
        deadline = asyncio.get_running_loop().time() + self.settings.PORTAL_TIMEOUT_MS / 1000
        while asyncio.get_running_loop().time() < deadline:
            if origin(page.url) != self.base_origin:
                raise RuntimeError(f'Unexpected portal origin during navigation: {page.url}')
            if await self._portal_is_ready(page):
                return 'portal'
            if await page.locator('#password').is_visible():
                if allow_login:
                    return 'login'
                raise RuntimeError(
                    f'Authentication is not active in this browser tab; '
                    f'portal redirected to {page.url}. Check session cookies, '
                    f'sessionStorage, account permissions and MFA.'
                )
            await asyncio.sleep(0.35)
        raise RuntimeError(
            f'Portal dashboard did not become visible after '
            f'{self.settings.PORTAL_TIMEOUT_MS}ms at {page.url}. '
            f'Check authentication, the portal URL and dashboard loading.'
        )

    async def _load_root(self, page: Page, *, allow_login: bool = False) -> str:
        await page.goto(
            self.settings.PORTAL_ENTRY_URL,
            wait_until='domcontentloaded',
            timeout=self.settings.PORTAL_TIMEOUT_MS,
        )
        return await self._wait_root_state(page, allow_login=allow_login)

    async def _login_feedback(self, page: Page) -> list[str]:
        selectors = (
            '[role="alert"], [aria-live="assertive"], '
            '[class*="error"], [class*="Error"], '
            '[class*="warning"], [class*="Warning"], '
            '[class*="toast"], [class*="Toast"]'
        )
        messages: list[str] = []
        try:
            for text in await page.locator(selectors).all_inner_texts():
                value = ' '.join(text.split()).strip()[:220]
                if value and value not in messages:
                    messages.append(value)
                if len(messages) >= 5:
                    break
        except Exception:
            pass
        if not messages:
            try:
                body = await page.locator('body').inner_text(timeout=2500)
                warning = re.compile(
                    r'\b(incorret|inv[aá]lid|bloquead|expirad|captcha|'
                    r'c[oó]digo de verifica[cç][aã]o|autenticador|'
                    r'multifator|n[aã]o foi poss[ií]vel|acesso negado|'
                    r'login failed|invalid credentials|password incorrect)'
                    , re.I
                )
                for line in body.splitlines():
                    line = ' '.join(line.split()).strip()
                    if 5 <= len(line) <= 220 and warning.search(line):
                        messages.append(line)
                        if len(messages) >= 5:
                            break
            except Exception:
                pass

        protected = [self.settings.PORTAL_PASSWORD, self.settings.PORTAL_USERNAME]
        sanitized: list[str] = []
        for value in messages:
            for secret in protected:
                if secret:
                    value = value.replace(secret, '[redacted]')
            sanitized.append(value)
        return sanitized

    async def _login_debug_screenshot(self, page: Page) -> str | None:
        if self.settings.APP_ENV != 'local':
            return None
        try:
            from pathlib import Path
            screenshot = Path(self.settings.NETWORK_SHARE_ROOT) / '_portal_login_debug.png'

            for selector in ('#user', '#password'):
                field = page.locator(selector)
                if await field.count():
                    await field.evaluate('(el) => { el.style.visibility = "hidden"; }')
            await page.screenshot(path=str(screenshot), full_page=False)
            return str(screenshot)
        except Exception as exc:
            logger.warning('login_screenshot_unavailable', error_type=type(exc).__name__)
            return None

    async def _confirm_popup_login(self, page: Page) -> None:
        try:
            logger.info('portal_login_popup_waiting', path=urlsplit(page.url).path)
            button_confirm = page.locator("#alertConfirm")
            await button_confirm.wait_for(state='visible', timeout=3500)
            logger.info('portal_login_popup_visible', path=urlsplit(page.url).path)
            await button_confirm.click()
        except PlaywrightTimeoutError:
            logger.warning('portal_login_popup_timeout', path=urlsplit(page.url).path)
            return

    async def _login(self, page: Page) -> None:
        state = await self._load_root(page, allow_login=True)
        if state == 'login':
            if not self.settings.PORTAL_USERNAME or not self.settings.PORTAL_PASSWORD:
                raise RuntimeError('Set PORTAL_USERNAME and PORTAL_PASSWORD')
            user_field = page.locator('#user')
            pass_field = page.locator('#password')
            await user_field.fill(self.settings.PORTAL_USERNAME)
            await pass_field.fill(self.settings.PORTAL_PASSWORD)

            if (await user_field.input_value() != self.settings.PORTAL_USERNAME or
                    await pass_field.input_value() != self.settings.PORTAL_PASSWORD):
                raise RuntimeError('Login form did not retain the configured credentials')
            button = page.get_by_role('button', name=re.compile(r'^\s*entrar\s*$', re.I))
            if await button.count():
                await button.first.click(timeout=self.settings.PORTAL_TIMEOUT_MS)
            else:
                await page.get_by_text('Entrar', exact=True).first.click(
                    timeout=self.settings.PORTAL_TIMEOUT_MS
                )
            logger.info('portal_login_clicked', path=urlsplit(page.url).path)
            await self._confirm_popup_login(page)

            deadline = asyncio.get_running_loop().time() + self.settings.PORTAL_TIMEOUT_MS / 1000
            while asyncio.get_running_loop().time() < deadline:
                if await self._portal_is_ready(page):
                    break
                password_visible = await page.locator('#password').is_visible()
                if not password_visible:

                    try:
                        await self._load_root(page, allow_login=False)
                        break
                    except RuntimeError:

                        pass
                await asyncio.sleep(0.5)
            else:
                feedback = await self._login_feedback(page)
                screenshot = await self._login_debug_screenshot(page)
                password_still_visible = await page.locator('#password').is_visible()
                logger.error(
                    'portal_login_not_completed',
                    path=urlsplit(page.url).path,
                    password_field_visible=password_still_visible,
                    feedback=feedback,
                    screenshot=screenshot,
                )
                raise RuntimeError(
                    'SoftExpert login did not complete after clicking Entrar. '
                    f'Login form visible={password_still_visible}; '
                    f'messages={feedback!r}; screenshot={screenshot or "unavailable"}. '
                    'Check credentials, account authorization, MFA and browser/network errors.'
                )

            if not await self._portal_is_ready(page):
                await self._load_root(page, allow_login=False)
        if not await self._portal_is_ready(page):
            raise RuntimeError('Login could not be confirmed: portal tabs are not visible')
        logger.info('portal_login_confirmed', path=urlsplit(page.url).path)

    async def _click_tab(self, page: Page, label: str) -> None:
        pattern = re.compile(r'^\s*' + re.escape(label) + r'\s*$')
        deadline = asyncio.get_running_loop().time() + self.settings.PORTAL_TIMEOUT_MS / 1000
        observed: set[str] = set()
        while asyncio.get_running_loop().time() < deadline:
            for frame in page.frames:
                try:
                    tabs = frame.locator(TAB_SELECTOR)

                    for index in range(min(await tabs.count(), 100)):
                        tab = tabs.nth(index)
                        text = ' '.join((await tab.inner_text(timeout=2000)).split())
                        observed.add(text[:80])
                        if not pattern.fullmatch(text):
                            continue

                        await tab.evaluate('''el => {
                            el.scrollIntoView({block: 'nearest', inline: 'center'});
                        }''')
                        try:
                            await tab.click(timeout=3500)
                        except Exception as click_error:



                            if not await tab.is_visible():
                                raise click_error
                            await tab.evaluate('''el => {
                                if (!el.getClientRects().length ||
                                    getComputedStyle(el).visibility === 'hidden') {
                                    throw new Error('Tab is hidden');
                                }
                                el.click();
                            }''')
                            logger.info('portal_tab_dom_click', tab=label)


                        class_name = await tab.get_attribute('class') or ''
                        if 'FolderTabItemContent' in class_name:
                            try:
                                await frame.locator(
                                    '[class*="FolderTabItemContent_activeTab"]'
                                ).filter(has_text=pattern).first.wait_for(
                                    state='visible', timeout=6000
                                )
                            except Exception as exc:
                                raise RuntimeError(
                                    f'Tab {label!r} was clicked but did not become active'
                                ) from exc
                        await self._settle_after_navigation(page)
                        logger.info('portal_tab_opened', tab=label,
                                    frames=len(page.frames))
                        return
                except Exception as exc:
                    if type(exc).__name__ in ('TimeoutError', 'Error'):

                        continue
                    raise
            await asyncio.sleep(0.35)
        raise RuntimeError(
            f'Tab {label!r} not found in {len(page.frames)} frame(s), '
            f'path={urlsplit(page.url).path}; '
            f'visible/discovered tabs={sorted(observed)[:20]!r}'
        )

    async def _click_portal(self, page: Page, action: Action) -> None:
        deadline = asyncio.get_running_loop().time() + self.settings.PORTAL_TIMEOUT_MS / 1000
        while asyncio.get_running_loop().time() < deadline:
            for frame in page.frames:
                try:
                    elements = frame.locator(PORTAL_SELECTOR)
                    filtered = elements.filter(has_text=action.label)
                    if await filtered.count() > action.occurrence:
                        tile = filtered.nth(action.occurrence)
                        await tile.evaluate('el => el.scrollIntoView({block:"center", inline:"center"})')
                        await tile.click(timeout=min(self.settings.PORTAL_TIMEOUT_MS, 8000))
                        await self._settle_after_navigation(page)
                        return
                    if await elements.count():
                        await elements.last.evaluate('''el => {
                            let p = el.parentElement;
                            while (p && p !== document.body &&
                                   p.scrollHeight <= p.clientHeight + 8) p=p.parentElement;
                            if (p) p.scrollTop += Math.max(240, p.clientHeight - 60);
                        }''')
                except Exception:
                    continue
            await page.wait_for_timeout(300)
        raise RuntimeError(f'Portal tile {action.label!r} not found after scrolling frames')

    async def _click(self, page: Page, action: Action) -> Page:
        existing_pages = set(page.context.pages)
        try:
            if action.kind == 'tab':
                await self._click_tab(page, action.label)
            else:
                await self._click_portal(page, action)
        except Exception as exc:
            details = str(exc).strip().replace('\n', ' ')[:600]
            raise RuntimeError(
                f'Unable to open {action.kind} {action.label!r}: '
                f'{type(exc).__name__}: {details}'
            ) from exc
        created = [p for p in page.context.pages if p not in existing_pages]
        if created:
            page = created[-1]
            try:
                await page.wait_for_load_state('domcontentloaded', timeout=self.settings.PORTAL_TIMEOUT_MS)
                await self._settle_after_navigation(page)
            except Exception:
                pass
        if origin(page.url) != self.base_origin:
            raise RuntimeError('Portal navigation redirected outside configured host')
        if await page.locator('#password').count():
            raise RuntimeError('Portal session expired')
        return page

    async def _open_node(self, entry: Page, node: Node) -> Page:
        page = entry
        try:
            await self._load_root(page, allow_login=False)
            for action in node.actions:
                previous = page
                page = await self._click(page, action)
                if previous is not page and previous is not entry:
                    await previous.close()
            return page
        except Exception:
            if page is not entry and not page.is_closed():
                await page.close()
            raise

    async def _tabs(self, page: Page, is_root: bool) -> list[str]:
        await self._wait_tab_dom(page)
        seen: set[str] = set()
        labels: list[str] = []

        for frame in page.frames:
            try:
                for label in await frame.locator(TAB_SELECTOR).all_inner_texts():
                    name = ' '.join(label.split()).strip()
                    if name and len(name) <= 100 and name not in seen:
                        seen.add(name)
                        labels.append(name)
            except Exception:
                continue

        if is_root and self.settings.root_tabs:
            allowed = set(self.settings.root_tabs)
            labels = [label for label in labels if label in allowed]
        logger.info('portal_tabs_discovered', count=len(labels), tabs=labels[:25],
                    frames=len(page.frames))
        return labels

    async def _subportals(self, page: Page) -> list[Action]:
        result: list[Action] = []
        seen: set[tuple[str, int]] = set()
        for frame in page.frames:
            stable = 0
            for _ in range(120):
                try:
                    elements = frame.locator(PORTAL_SELECTOR)
                    items = await elements.evaluate_all(r"""nodes => nodes.filter(node => {
                        for (let el = node; el && el.nodeType === 1; el = el.parentElement) {
                            const style = getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden') return false;
                        }
                        return node.getClientRects().length > 0;
                    }).map(node => (node.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 180)).filter(Boolean)""")
                    new_count = 0
                    local_counts: dict[str, int] = {}
                    for label in items:
                        occurrence = local_counts.get(label, 0)
                        local_counts[label] = occurrence + 1
                        key = (label, occurrence)
                        if key not in seen:
                            seen.add(key)
                            result.append(Action('portal', label, occurrence))
                            new_count += 1
                    if not items:
                        break
                    stable = stable + 1 if not new_count else 0
                    if stable >= 3:
                        break
                    await elements.last.evaluate("""el => {
                        let parent = el.parentElement;
                        while (parent && parent !== document.body &&
                               parent.scrollHeight <= parent.clientHeight + 8) parent = parent.parentElement;
                        if (parent) parent.scrollTop += Math.max(240, Math.floor(parent.clientHeight * .75));
                    }""")
                    await page.wait_for_timeout(350)
                except Exception as exc:
                    logger.warning('portal_list_read_failed', reason=type(exc).__name__, detail=str(exc)[:200])
                    break
        return result

    async def _reports(self, page: Page, node: Node) -> list[ReportDescriptor]:
        data: list[dict] = []
        for frame in page.frames:
            try:
                data.extend(await frame.locator('tr').evaluate_all(r"""rows => {
                    const visible = node => {
                        for (let el = node; el && el.nodeType === 1; el = el.parentElement) {
                            const style = getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden') return false;
                        }
                        return node.getClientRects().length > 0;
                    };
                    return rows.filter(visible).flatMap(row => {
                        const cells = [...row.querySelectorAll('td')];
                        if (!cells.length) return [];
                        const cell = cells.find(c => /gerar\s*pdf/i.test(c.innerText || ''));
                        if (!cell) return [];
                        const table = row.closest('table');
                        const headers = [...(table?.querySelectorAll('thead th') || [])]
                            .map(x => (x.innerText || '').trim().toLowerCase());
                        const ix = (...terms) => headers.findIndex(h => terms.some(t => h.includes(t)));
                        const idxCode = ix('controle', 'código', 'codigo', 'n° fnc', 'nº fnc');
                        const idxUnit = ix('unidade', 'local');
                        const idxDate = ix('data da', 'data de', 'data ', 'verificação', 'verificacao');
                        const fallbackDate = cells.map(c => (c.innerText || '').trim()).find(
                            value => /[0-9]{1,2}\/[0-9]{1,2}\/[0-9]{4}/.test(value)
                        ) || '';
                        const first = (cells[0]?.innerText || '').trim();
                        const id = (first.match(/\b\d{3,12}\b/) || [''])[0];
                        return [...cell.querySelectorAll('a[href]')].map(a => ({
                            url: new URL(a.getAttribute('href'), document.baseURI).href,
                            record_id: id,
                            code: (cells[idxCode >= 0 ? idxCode : 5]?.innerText || '').trim(),
                            unit: (cells[idxUnit >= 0 ? idxUnit : 7]?.innerText || '').trim(),
                            date: (idxDate >= 0 ? (cells[idxDate]?.innerText || fallbackDate) : fallbackDate).trim()
                        }));
                    });
                }"""))
            except Exception as exc:
                logger.warning('report_table_read_failed', reason=type(exc).__name__, detail=str(exc)[:180])
        reports: list[ReportDescriptor] = []
        for item in data:
            url = item['url']
            if not allowed_report_url(url, self.allowlist):
                self.issues.append('Report URL rejected by allowlist')
                continue
            doc_id = item['record_id'] or record_identifier(url)
            reports.append(ReportDescriptor(
                url=url, document_id=doc_id[:255], portal_path=node.trail,
                tab=node.tab, code=item['code'][:200], unit=item['unit'][:200], date=item['date'][:100],
            ))
        return reports

    async def _row_signature(self, page: Page) -> tuple[str, ...]:
        signatures: list[str] = []
        for frame in page.frames:
            try:
                items = await frame.locator('tr').evaluate_all(r"""rows => rows.filter(row => {
                    const cell = row.querySelector('td');
                    return cell && /\d{3,12}/.test(cell.innerText || '');
                }).map(row => {
                    const first = row.querySelector('td');
                    const report = [...row.querySelectorAll('td')].find(td => /gerar\s*pdf/i.test(td.innerText || ''));
                    const href = report?.querySelector('a[href]')?.getAttribute('href') || '';
                    return `${(first?.innerText || '').trim().slice(0, 40)}|${href}`;
                }).slice(-15)""")
                signatures.extend(items)
            except Exception:
                continue
        return tuple(signatures)

    async def _load_more(self, page: Page, before: tuple[str, ...]) -> bool:
        for frame in page.frames:
            try:
                rows = frame.locator('tr').filter(has_text=re.compile(r'carregar mais registros', re.I))
                for index in range(min(await rows.count(), 5)):
                    row = rows.nth(index)
                    button = row.get_by_text('Clique aqui', exact=True)
                    if not await button.count() or not await button.first.is_visible():
                        continue
                    await button.first.click(timeout=8000)
                    deadline = asyncio.get_running_loop().time() + min(20, self.settings.PORTAL_TIMEOUT_MS / 1000)
                    while asyncio.get_running_loop().time() < deadline:
                        await page.wait_for_timeout(450)
                        if await self._row_signature(page) != before:
                            await self._settle_after_navigation(page)
                            return True
                    raise RuntimeError('Load more was clicked but the table did not advance')
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(f'Unable to load more rows: {type(exc).__name__}: {str(exc)[:200]}') from exc
        return False

    async def _scroll_dashboards(self, page: Page) -> None:
        for frame in page.frames:
            try:
                cards = frame.locator(".dashboardCard:visible")

                for index in range(await cards.count()):
                    await cards.nth(index).scroll_into_view_if_needed()
                    await asyncio.sleep(0.15)
            except PlaywrightTimeoutError as exc:
                logger.warning('dashboard_scroll_timeout', reason=type(exc).__name__, detail=str(exc)[:200])
            await page.wait_for_timeout(300)

    async def crawl(self, job: SyncJob,
                    on_report: Callable[[ReportDescriptor, BrowserContext], Awaitable[None]],
                    heartbeat: Callable[[], Awaitable[None]]) -> int:
        opts = job.options
        max_depth = opts.get('max_depth', 3 if job.mode == 'test' else 12)
        max_portals = opts.get('max_portals', 3 if job.mode == 'test' else 0)
        max_reports = opts.get('max_reports', 5 if job.mode == 'test' else 0)
        visited_nodes: set[tuple[Action, ...]] = set()
        visited_reports: set[str] = set()
        queue: deque[Node] = deque([Node()])
        portal_count = 0
        node_count = 0

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
            context = await browser.new_context(accept_downloads=True, viewport={"width": 1600, "height": 960})
            try:
                entry = await context.new_page()
                await self._login(entry)
                logger.info('portal_frames_after_login', frame_count=len(entry.frames),
                            paths=[urlsplit(f.url).path for f in entry.frames][:12])
                while queue:
                    node = queue.popleft()
                    if node.actions in visited_nodes or node.depth > max_depth:
                        continue


                    is_portal_node = not node.actions or node.actions[-1].kind == 'portal'
                    if is_portal_node and max_portals and portal_count >= max_portals:
                        continue
                    visited_nodes.add(node.actions)
                    node_count += 1
                    if is_portal_node:
                        portal_count += 1
                    await heartbeat()
                    page = None
                    try:
                        page = await self._open_node(entry, node)
                        logger.info('scraping_node', level=node.depth, trail=node.trail, tab=node.tab)

                        if not node.actions or node.actions[-1].kind == 'portal':
                            tabs = await self._tabs(page, is_root=not node.actions)
                            for tab in tabs:
                                child = Node(node.actions + (Action('tab', tab),), node.trail,
                                             node.depth, tab)
                                if child.actions not in visited_nodes:
                                    queue.append(child)
                        reports_in_node = 0
                        for page_number in range(2000):
                            await heartbeat()
                            for report in await self._reports(page, node):
                                identity = source_identity(report.url)
                                if identity in visited_reports:
                                    continue
                                visited_reports.add(identity)
                                reports_in_node += 1
                                await on_report(report, context)
                                if max_reports and len(visited_reports) >= max_reports:
                                    break
                            if max_reports and len(visited_reports) >= max_reports:
                                break
                            before = await self._row_signature(page)
                            try:
                                if not await self._load_more(page, before):
                                    break
                                logger.info('portal_more_records_loaded', tab=node.tab,
                                            trail=node.trail, page=page_number + 2)
                            except Exception as exc:
                                self.issues.append(
                                    f'Pagination incomplete in {node.trail}/{node.tab}: {type(exc).__name__}: {str(exc)[:180]}'
                                )
                                logger.warning('portal_pagination_failed', tab=node.tab,
                                               trail=node.trail, detail=str(exc)[:260])
                                break
                        else:
                            self.issues.append('Pagination reached safety cap of 2000 clicks')
                        if reports_in_node == 0:
                            logger.info('portal_tab_no_reports', trail=node.trail, tab=node.tab)
                        if max_reports and len(visited_reports) >= max_reports:
                            break
                        if node.depth < max_depth:
                            children = await self._subportals(page)
                            if children:
                                logger.info('portal_children_found', trail=node.trail,
                                            tab=node.tab, count=len(children))
                            for child_action in children:
                                child_node = Node(node.actions + (child_action,),
                                                  node.trail + (child_action.label,),
                                                  node.depth + 1, 'Geral')
                                if child_node.actions not in visited_nodes:
                                    queue.append(child_node)
                    except Exception as exc:
                        message = f'Navigation failed at {node.trail}: {type(exc).__name__}: {exc}'
                        self.issues.append(message[:500])
                        logger.warning('portal_navigation_failed', trail=node.trail,
                                       tab=node.tab,
                                       actions=[f'{a.kind}:{a.label}' for a in node.actions],
                                       reason=type(exc).__name__,
                                       detail=str(exc)[:700])
                    finally:
                        if page is not None and page is not entry and not page.is_closed():
                            await page.close()
                self.portals_seen = portal_count
                self.reports_seen = len(visited_reports)
                logger.info('crawl_finished', nodes=node_count, portals=portal_count,
                            reports=self.reports_seen,
                            errors=len(self.issues))
                return len(visited_reports)
            finally:
                await context.close()
                await browser.close()
