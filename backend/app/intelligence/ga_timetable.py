"""Read PrimeFlow's dated print view and attach it only to the morning report."""
import asyncio
import hashlib
import io
import json
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from app.config import settings
from app.mail import EmailAttachment

ZONE = ZoneInfo('Europe/Budapest')
MAX_PDF_BYTES = 8 * 1024 * 1024


def morning_enabled(phase: str) -> bool:
    return phase == 'scheduled-am' and bool(settings.NEWS_GA_TIMETABLE_BINDING_FILE)


def load_binding() -> dict:
    binding = json.loads(Path(settings.NEWS_GA_TIMETABLE_BINDING_FILE).read_text(encoding='utf-8-sig'))
    if binding['recipient'].strip().casefold() != settings.NEWS_EMAIL_RECIPIENT.strip().casefold():
        raise ValueError('GA attachment recipient binding changed')
    if binding.get('username') != 'gane.arifaj':
        raise ValueError('GA attachment requires the bound GA account')
    for key in ('primeflow_python', 'primeflow_backend', 'browser_executable', 'cache_directory'):
        if not Path(binding[key]).is_absolute():
            raise ValueError('GA attachment paths must be absolute')
    origin = urlsplit(binding['origin'])
    if origin.scheme != 'https' or not origin.hostname or origin.username or origin.password or origin.path not in ('', '/'):
        raise ValueError('GA attachment requires an HTTPS origin')
    return binding


def validate_pdf(data: bytes, day: date) -> None:
    if not data.startswith(b'%PDF-') or len(data) > MAX_PDF_BYTES:
        raise ValueError('Invalid GA PDF')
    pages = PdfReader(io.BytesIO(data)).pages
    if len(pages) != 1:
        raise ValueError('GA timetable must fit on one page')
    text = pages[0].extract_text()
    if f'GA TIME TABLE ({day:%d.%m.%Y})' not in text or 'KOMENT' not in text:
        raise ValueError('GA PDF date or table missing')


async def export_session(binding: dict) -> dict:
    process = await asyncio.create_subprocess_exec(binding['primeflow_python'],
        str(Path(__file__).with_name('ga_timetable_auth.py')), binding['username'],
        cwd=binding['primeflow_backend'], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise RuntimeError('GA export session unavailable')
    session = json.loads(stdout)
    if session['user']['username'] != binding['username']:
        raise ValueError('Unexpected GA export identity')
    return session


async def render_pdf(binding: dict, day: date) -> bytes:
    from playwright.async_api import Error as PlaywrightError, async_playwright

    session = await export_session(binding)
    origin = binding['origin'].rstrip('/')
    allowed = {origin, *binding.get('api_origins', [])}
    week = day.fromordinal(day.toordinal() - day.weekday()).isoformat()
    loaded = set()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=binding['browser_executable'])
        try:
            context = await browser.new_context(timezone_id='Europe/Budapest', locale='en-GB',
                                                viewport={'width': 1600, 'height': 1100}, service_workers='block')
            # A fresh context prevents cached rows from concealing a failed read.
            await context.add_init_script('''(config => {
                if (location.origin === config.origin) {
                    localStorage.setItem('primex_access_token', config.session.token);
                    localStorage.setItem('primex_user', JSON.stringify(config.session.user));
                    window.print = () => {};
                }
            })(''' + json.dumps({'origin': origin, 'session': session}) + ')')

            async def readonly(route):
                request = route.request
                parts = urlsplit(request.url)
                request_origin = f'{parts.scheme}://{parts.netloc}'
                try:
                    if request.method not in {'GET', 'HEAD', 'OPTIONS'} or request_origin not in allowed:
                        await route.abort()
                    else:
                        await route.continue_()
                except PlaywrightError:
                    pass  # The export may close while unrelated page reads finish.

            await context.route('**/*', readonly)
            await context.route_web_socket('**/*', lambda ws: ws.close())
            page = await context.new_page()
            page.set_default_timeout(60000)

            async def response_ready(response):
                parts = urlsplit(response.url)
                path = parts.path.rstrip('/')
                query = parse_qs(parts.query)
                name = None
                if path.endswith('/ga-time-slots/rows'):
                    name = 'rows'
                elif path.endswith('/ga-time-slots') and query.get('week_start') == [week]:
                    name = 'slots'
                elif path.endswith('/common-view') and query.get('week_start') == [week]:
                    name = 'meetings'
                if name and response.ok:
                    try:
                        await response.finished()
                        loaded.add(name)
                    except PlaywrightError:
                        pass

            page.on('response', response_ready)
            await page.goto(origin + '/admin-tasks#ga-time-table', wait_until='domcontentloaded', timeout=60000)
            field = page.get_by_role('textbox', name='GA timetable day', exact=True)
            await field.fill(day.strftime('%d/%m/%Y'))
            await field.press('Tab')
            async def wait_for_data():
                while loaded != {'rows', 'slots', 'meetings'}:
                    await asyncio.sleep(0.1)
            await asyncio.wait_for(wait_for_data(), timeout=90)
            await page.wait_for_function('''() => !document.body.innerText.includes('Loading GA time slots...')''')
            await page.evaluate('document.fonts.ready')
            section = page.locator('[data-print-section="ga-time"]')
            if await section.locator('table.ga-time-single-day').count() != 2:
                raise ValueError('GA single-day view missing')
            if await section.locator('.print-title').inner_text() != f'GA TIME TABLE ({day:%d.%m.%Y})':
                raise ValueError('GA print date mismatch')
            last_cells = await section.locator('.print-only tbody tr').last.locator('td').all_inner_texts()
            # Sets the existing print target through its own Print button.
            await page.locator('#ga-time-table').get_by_role('button', name='Print', exact=True).click()
            await page.wait_for_selector('[data-print-target="ga-time"]')
            await page.emulate_media(media='print')
            await page.evaluate('''() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))''')
            # Freeze only the print root, excluding app-shell notices and portals.
            await page.evaluate('''() => {
                const root = document.querySelector('[data-print-target="ga-time"]').cloneNode(true);
                const styles = [...document.querySelectorAll('style')].map(node => node.cloneNode(true));
                document.body.replaceChildren(root, ...styles);
            }''')
            for scale in (0.8, 0.7, 0.6):
                data = await page.pdf(format='A4', landscape=True, scale=scale,
                                      print_background=True, prefer_css_page_size=True)
                if len(PdfReader(io.BytesIO(data)).pages) == 1:
                    break
            text = ''.join(PdfReader(io.BytesIO(data)).pages[0].extract_text().split())
            if any(''.join(cell.split()) not in text for cell in last_cells if cell.strip()):
                raise ValueError('GA timetable last row is missing from PDF')
        finally:
            await browser.close()
    validate_pdf(data, day)
    return data


async def morning_attachment(phase: str, cutoff: datetime) -> EmailAttachment | None:
    if not morning_enabled(phase):
        return None
    binding = load_binding()
    day = cutoff.astimezone(ZONE).date()
    folder = Path(binding['cache_directory'])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'GA-Time-Table-{day.isoformat()}.pdf'
    if path.exists():
        data = path.read_bytes()
        validate_pdf(data, day)
    else:
        data = await asyncio.wait_for(render_pdf(binding, day), timeout=150)
        # Atomic creation: an interrupted export never becomes tomorrow's input.
        temporary = path.with_suffix('.pdf.tmp')
        temporary.write_bytes(data)
        temporary.replace(path)
    return EmailAttachment(path.name, data)


def attachment_metadata(attachment: EmailAttachment) -> dict:
    return {'filename': attachment.filename, 'content_type': attachment.content_type,
            'sha256': hashlib.sha256(attachment.data).hexdigest(), 'bytes': len(attachment.data)}
