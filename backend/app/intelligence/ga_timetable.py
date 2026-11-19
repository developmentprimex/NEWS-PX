"""Attach PrimeFlow's dated print view to the morning and evening reports."""
import asyncio
import hashlib
import io
import json
import math
import zipfile
from xml.etree import ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
from pypdf import PdfReader

from app.config import settings
from app.mail import EmailAttachment

ZONE = ZoneInfo('Europe/Budapest')
MAX_PDF_BYTES = 8 * 1024 * 1024
XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
SHEET_NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def morning_enabled(phase: str) -> bool:
    return phase in {'scheduled-am', 'scheduled-pm'} and bool(settings.NEWS_GA_TIMETABLE_BINDING_FILE)


def slot_binding(phase: str) -> dict:
    binding = load_binding()
    if phase == 'scheduled-pm':
        # The evening report captures later edits instead of reusing 07:00 files.
        binding = dict(binding, cache_directory=str(Path(binding['cache_directory']) / 'scheduled-pm'))
    return binding


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
            table_rows = await section.locator('.print-only tbody tr').evaluate_all('''rows => rows.map(row => {
                const cells = [...row.cells];
                if (cells.length === 3 && cells[1].colSpan === 2)
                    return [cells[0].innerText, '', cells[1].innerText, cells[2].innerText];
                return cells.map(cell => cell.innerText);
            })''')
            if not table_rows or any(len(row) != 4 for row in table_rows):
                raise ValueError('GA daily table must have four columns')
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
    folder = Path(binding['cache_directory'])
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / f'GA-Time-Table-{day.isoformat()}.rows.json'
    temporary = snapshot.with_suffix('.json.tmp')
    temporary.write_text(json.dumps({'version': 2, 'date': day.isoformat(), 'rows': table_rows}, ensure_ascii=False), encoding='utf-8')
    temporary.replace(snapshot)
    return data


async def morning_attachment(phase: str, cutoff: datetime) -> EmailAttachment | None:
    if not morning_enabled(phase):
        return None
    binding = slot_binding(phase)
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


def validate_excel(data: bytes, day: date, rows: list[list[str]] | None = None) -> None:
    """Reject login/error responses, full-week exports and incorrect report dates."""
    if len(data) > MAX_PDF_BYTES or not zipfile.is_zipfile(io.BytesIO(data)):
        raise ValueError('Invalid GA Excel export')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 32 * 1024 * 1024:
            raise ValueError('GA Excel export is too large')
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        if len(workbook.findall('s:sheets/s:sheet', SHEET_NS)) != 1:
            raise ValueError('GA Excel must contain one daily sheet')
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            shared = ET.fromstring(archive.read('xl/sharedStrings.xml'))
            strings = [''.join(node.itertext()) for node in shared.findall('s:si', SHEET_NS)]
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        cells = {}
        for cell in sheet.findall('.//s:sheetData/s:row/s:c', SHEET_NS):
            if cell.find('s:f', SHEET_NS) is not None:
                raise ValueError('Unexpected formula in GA timetable export')
            value = cell.findtext('s:v', default='', namespaces=SHEET_NS)
            if cell.get('t') == 's':
                value = strings[int(value)]
            elif cell.get('t') == 'inlineStr':
                value = ''.join(t.text or '' for t in cell.findall('.//s:t', SHEET_NS))
            cells[cell.get('r')] = value
        if (cells.get('A1') != f'GA TIME TABLE ({day:%d.%m.%Y})'
                or [cells.get(f'{col}4') for col in 'ABC'] != ['NR', 'TIME', 'KOMENT']
                or day.strftime('%d.%m.%Y') not in cells.get('D4', '')):
            raise ValueError('GA Excel date or table missing')
        if any(ref.rstrip('0123456789') not in {'A', 'B', 'C', 'D'} and value
               for ref, value in cells.items()):
            raise ValueError('GA Excel must be filtered to one day')
        if rows is not None:
            for index, row in enumerate(rows, 5):
                for col, value in zip('ABCD', row):
                    if cells.get(f'{col}{index}', '') != value:
                        raise ValueError('GA Excel differs from the printed daily table')


def excel_with_printed_rows(data: bytes, rows: list[list[str]]) -> bytes:
    """Keep the native Excel layout; fill the same comments and meetings as Print."""
    ns = SHEET_NS['s']
    ET.register_namespace('', ns)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, 'w') as target:
        sheet = ET.fromstring(source.read('xl/worksheets/sheet1.xml'))
        data_rows = sheet.findall('s:sheetData/s:row', SHEET_NS)
        data_rows = [row for row in data_rows if int(row.get('r')) >= 5]
        if len(data_rows) != len(rows):
            raise ValueError('GA Excel row structure differs from Print')
        for row_node, values in zip(data_rows, rows):
            line_count = 1
            for col, width, value in zip('ABCD', (5, 16, 22, 40), values):
                ref = col + row_node.get('r')
                cell = next((cell for cell in row_node if cell.get('r') == ref), None)
                if cell is None:
                    cell = ET.SubElement(row_node, '{' + ns + '}c', {'r': ref})
                for child in list(cell):
                    cell.remove(child)
                cell.set('t', 'inlineStr')
                text = ET.SubElement(ET.SubElement(cell, '{' + ns + '}is'), '{' + ns + '}t')
                text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                text.text = value
                line_count = max(line_count, sum(max(1, math.ceil(len(line) / width)) for line in value.split('\n')))
            row_node.set('ht', str(min(409, max(18, 14 * line_count + 4))))
            row_node.set('customHeight', '1')
        for item in source.infolist():
            content = ET.tostring(sheet, encoding='utf-8') if item.filename == 'xl/worksheets/sheet1.xml' else source.read(item.filename)
            target.writestr(item, content)
    return output.getvalue()


async def download_excel(binding: dict, day: date) -> bytes:
    folder = Path(binding['cache_directory'])
    snapshot = folder / f'GA-Time-Table-{day.isoformat()}.rows.json'
    if not snapshot.exists() or json.loads(snapshot.read_text(encoding='utf-8')).get('version') != 2:
        # Upgrade old PDF-only caches to a common frozen Print/Excel snapshot.
        pdf = await render_pdf(binding, day)
        pdf_path = folder / f'GA-Time-Table-{day.isoformat()}.pdf'
        temporary = pdf_path.with_suffix('.pdf.tmp')
        temporary.write_bytes(pdf)
        temporary.replace(pdf_path)
    model = json.loads(snapshot.read_text(encoding='utf-8'))
    if (model['date'] != day.isoformat() or not model['rows']
            or any(len(row) != 4 or not all(isinstance(value, str) for value in row) for row in model['rows'])):
        raise ValueError('GA table snapshot date mismatch')
    session = await export_session(binding)
    api = binding['api_origins'][0].rstrip('/')
    parts = urlsplit(api)
    if parts.scheme != 'https' or parts.path or parts.query or parts.username or parts.password:
        raise ValueError('GA Excel requires a bound HTTPS API origin')
    week = day.fromordinal(day.toordinal() - day.weekday()).isoformat()
    async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
        response = await client.get(api + '/api/exports/ga-time.xlsx',
            params={'week_start': week, 'selected_date': day.isoformat()},
            headers={'Authorization': 'Bearer ' + session['token']})
        response.raise_for_status()
        data = response.content
    validate_excel(data, day)
    data = excel_with_printed_rows(data, model['rows'])
    validate_excel(data, day, model['rows'])
    return data


async def morning_attachments(phase: str, cutoff: datetime) -> list[EmailAttachment]:
    if not morning_enabled(phase):
        return []
    binding = slot_binding(phase)
    day = cutoff.astimezone(ZONE).date()
    folder = Path(binding['cache_directory'])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'GA-Time-Table-{day.isoformat()}.xlsx'
    snapshot = folder / f'GA-Time-Table-{day.isoformat()}.rows.json'
    data = None
    if path.exists() and snapshot.exists() and json.loads(snapshot.read_text(encoding='utf-8')).get('version') == 2:
        model = json.loads(snapshot.read_text(encoding='utf-8'))
        if model['date'] != day.isoformat():
            raise ValueError('GA table snapshot date mismatch')
        cached = path.read_bytes()
        try:
            validate_excel(cached, day, model['rows'])
            data = cached
        except ValueError:
            pass  # Rebuild an old native export from the already frozen Print rows.
    if data is None:
        data = await download_excel(binding, day)
        temporary = path.with_suffix('.xlsx.tmp')
        temporary.write_bytes(data)
        temporary.replace(path)
    pdf = await morning_attachment(phase, cutoff)
    return [pdf, EmailAttachment(path.name, data, XLSX_TYPE)]


def attachment_metadata(attachment: EmailAttachment) -> dict:
    return {'filename': attachment.filename, 'content_type': attachment.content_type,
            'sha256': hashlib.sha256(attachment.data).hexdigest(), 'bytes': len(attachment.data)}
