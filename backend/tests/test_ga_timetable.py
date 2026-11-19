import asyncio
import io
import json
import zipfile
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from app.config import settings
from app.intelligence import ga_timetable as service, digest_service
from app.mail import EmailAttachment, GmailService


def excel(day=date(2026, 10, 28), weekly=False, formula=False):
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    cells={'A1':f'GA TIME TABLE ({day:%d.%m.%Y})','A4':'NR','B4':'TIME','C4':'KOMENT',
           'D4':f'MR = {day:%d.%m.%Y}','D5':'Wednesday activity'}
    if weekly: cells['E4']='Another day'
    from xml.sax.saxutils import escape
    groups={}
    for ref,value in cells.items():
        groups.setdefault(ref.lstrip('ABCD E').strip(),[]).append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
    entries=''.join(f'<row r="{index}">{"".join(parts)}</row>' for index,parts in sorted(groups.items(),key=lambda pair:int(pair[0])))
    if formula: entries+='<row r="6"><c r="D6"><f>1+1</f><v>2</v></c></row>'
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}"><sheets><sheet name="GA Time Table" sheetId="1"/></sheets></workbook>')
        archive.writestr('xl/worksheets/sheet1.xml',f'<worksheet xmlns="{ns}"><sheetData>{entries}</sheetData></worksheet>')
    return output.getvalue()


def pdf(day=date(2026, 10, 28), pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=842, height=595)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 12 Tf 20 560 Td (GA TIME TABLE \\({day:%d.%m.%Y}\\) KOMENT) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_requires_exact_report_date_and_one_page():
    service.validate_pdf(pdf(), date(2026, 10, 28))
    for invalid in (pdf(date(2026, 10, 27)), pdf(pages=2), b'error html'):
        with pytest.raises(ValueError):
            service.validate_pdf(invalid, date(2026, 10, 28))


@pytest.mark.parametrize('phase', ['scheduled-day', 'manual-am', 'recovery-day'])
def test_non_report_slots_never_load_binding_or_export(monkeypatch, phase):
    monkeypatch.setattr(settings, 'NEWS_GA_TIMETABLE_BINDING_FILE', 'private-binding')
    monkeypatch.setattr(service, 'load_binding', lambda: pytest.fail('Collection-only slots must not read PrimeFlow'))
    assert asyncio.run(service.morning_attachment(phase, datetime.now(timezone.utc))) is None


def test_recipient_binding_is_checked_before_export(tmp_path, monkeypatch):
    path = tmp_path/'binding.json'
    path.write_text(json.dumps({'recipient': 'someone-else@example.com'}))
    monkeypatch.setattr(settings, 'NEWS_GA_TIMETABLE_BINDING_FILE', str(path))
    with pytest.raises(ValueError, match='recipient'):
        service.load_binding()


@pytest.mark.parametrize('cutoff,day', [
    ('2026-10-28T06:00:00+00:00', date(2026, 10, 28)),
    ('2027-03-28T05:00:00+00:00', date(2027, 3, 28)),
])
def test_dated_pdf_is_cached_without_regenerating_or_reusing_another_day(tmp_path, monkeypatch, cutoff, day):
    monkeypatch.setattr(settings, 'NEWS_GA_TIMETABLE_BINDING_FILE', 'private-binding')
    monkeypatch.setattr(service, 'load_binding', lambda: {'cache_directory': str(tmp_path)})
    render = AsyncMock(return_value=pdf(day))
    monkeypatch.setattr(service, 'render_pdf', render)
    async def run():
        stamp = datetime.fromisoformat(cutoff)
        first = await service.morning_attachment('scheduled-am', stamp)
        second = await service.morning_attachment('scheduled-am', stamp)
        assert first == second
        assert first.filename == f'GA-Time-Table-{day}.pdf'
        assert render.await_count == 1
        assert render.await_args.args[1] == day
        assert not list(tmp_path.glob('*.tmp'))
    asyncio.run(run())


def test_transport_emits_real_pdf_mime_attachment(monkeypatch):
    messages = []
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ehlo(self): pass
        def login(self, *args): pass
        def send_message(self, message, **kwargs): messages.append(message)
    monkeypatch.setattr('app.mail.smtplib.SMTP_SSL', SMTP)
    monkeypatch.setattr(settings, 'EMAIL_PORT', 465)
    attachment = EmailAttachment('GA-Time-Table-2026-10-28.pdf', pdf())
    mailer = GmailService(credentials=('test@example.com', 'test-password'))
    spreadsheet = EmailAttachment('GA-Time-Table-2026-10-28.xlsx', excel(), service.XLSX_TYPE)
    asyncio.run(mailer.send_verified('07:00', ['recipient@example.com'], 'News', html='<p>News</p>', attachments=[attachment, spreadsheet]))
    assert len(messages) == 1
    parts = list(messages[0].iter_attachments())
    assert len(parts) == 2 and parts[0].get_content_type() == 'application/pdf'
    assert parts[0].get_filename() == attachment.filename
    assert parts[0].get_payload(decode=True) == attachment.data
    assert parts[1].get_content_type() == service.XLSX_TYPE
    assert parts[1].get_payload(decode=True) == spreadsheet.data
    assert messages[0].get_body(preferencelist=('html',)).get_content().strip() == '<p>News</p>'


def test_digest_forwards_attachment_to_the_existing_transport(monkeypatch):
    send = AsyncMock()
    from types import SimpleNamespace
    monkeypatch.setattr(digest_service, 'GmailService', lambda: SimpleNamespace(host='smtp.gmail.com', port=465, send_verified=send))
    attachment = EmailAttachment('GA.pdf', pdf())
    asyncio.run(digest_service._send_digest('07:00', 'News', '<p>News</p>', attachments=[attachment]))
    assert send.await_args.kwargs['attachments'] == [attachment]


def test_excel_rejects_wrong_date_weekly_export_formulas_and_login_pages():
    service.validate_excel(excel(),date(2026,10,28))
    for invalid in (excel(date(2026,10,27)),excel(weekly=True),excel(formula=True),b'<html>Login</html>'):
        with pytest.raises(ValueError):
            service.validate_excel(invalid,date(2026,10,28))


def test_download_excel_requests_exact_day_and_includes_printed_meetings(tmp_path,monkeypatch):
    import httpx
    payload=excel()
    requests=[]
    async def respond(request):
        requests.append(request)
        return httpx.Response(200,content=payload,headers={'content-type':service.XLSX_TYPE})
    original=httpx.AsyncClient
    rows=[['1','08:00 - 09:00','=2+2','Wednesday activity\nTAK EXT: Meeting']]
    (tmp_path/'GA-Time-Table-2026-10-28.rows.json').write_text(json.dumps({'version':2,'date':'2026-10-28','rows':rows}))
    monkeypatch.setattr(service,'export_session',AsyncMock(return_value={'token':'test-token'}))
    monkeypatch.setattr(service.httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(respond)))
    result=asyncio.run(service.download_excel({'api_origins':['https://api.example.com'],'cache_directory':str(tmp_path)},date(2026,10,28)))
    service.validate_excel(result,date(2026,10,28),rows)
    assert len(requests)==1 and requests[0].method=='GET'
    assert requests[0].url.path=='/api/exports/ga-time.xlsx'
    assert dict(requests[0].url.params)=={'week_start':'2026-10-26','selected_date':'2026-10-28'}
    assert requests[0].headers['authorization']=='Bearer test-token'


def test_pdf_and_excel_are_cached_independently_for_both_report_slots(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'NEWS_GA_TIMETABLE_BINDING_FILE','private-binding')
    monkeypatch.setattr(service,'load_binding',lambda:{'cache_directory':str(tmp_path)})
    render=AsyncMock(return_value=pdf())
    download=AsyncMock(return_value=excel())
    (tmp_path/'GA-Time-Table-2026-10-28.rows.json').write_text(json.dumps({'version':2,'date':'2026-10-28','rows':[['','','','Wednesday activity']]}))
    evening = tmp_path/'scheduled-pm'
    evening.mkdir()
    (evening/'GA-Time-Table-2026-10-28.rows.json').write_text(json.dumps({'version':2,'date':'2026-10-28','rows':[['','','','Wednesday activity']]}))
    monkeypatch.setattr(service,'render_pdf',render)
    monkeypatch.setattr(service,'download_excel',download)
    async def run():
        stamp=datetime(2026,10,28,6,tzinfo=timezone.utc)
        first=await service.morning_attachments('scheduled-am',stamp)
        second=await service.morning_attachments('scheduled-am',stamp)
        assert first==second and len(first)==2
        assert [item.filename for item in first]==['GA-Time-Table-2026-10-28.pdf','GA-Time-Table-2026-10-28.xlsx']
        assert first[1].content_type==service.XLSX_TYPE
        assert download.await_count==render.await_count==1
        pm_stamp=datetime(2026,10,28,20,tzinfo=timezone.utc)
        pm_first=await service.morning_attachments('scheduled-pm',pm_stamp)
        pm_second=await service.morning_attachments('scheduled-pm',pm_stamp)
        assert pm_first==pm_second and len(pm_first)==2
        assert download.await_count==render.await_count==2
        assert render.await_args.args[0]['cache_directory']==str(evening)
        assert (tmp_path/'GA-Time-Table-2026-10-28.pdf').exists()
        assert (evening/'GA-Time-Table-2026-10-28.pdf').exists()
    asyncio.run(run())


@pytest.mark.parametrize('phase',['scheduled-am','scheduled-pm'])
def test_disabled_binding_skips_both_reports(monkeypatch,phase):
    monkeypatch.setattr(settings,'NEWS_GA_TIMETABLE_BINDING_FILE','')
    assert asyncio.run(service.morning_attachments(phase,datetime.now(timezone.utc)))==[]


def test_excel_failure_prevents_partial_attachment_bundle(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'NEWS_GA_TIMETABLE_BINDING_FILE','private-binding')
    monkeypatch.setattr(service,'load_binding',lambda:{'cache_directory':str(tmp_path)})
    monkeypatch.setattr(service,'render_pdf',AsyncMock(return_value=pdf()))
    monkeypatch.setattr(service,'download_excel',AsyncMock(side_effect=RuntimeError('Export unavailable')))
    with pytest.raises(RuntimeError):
        asyncio.run(service.morning_attachments('scheduled-am',datetime(2026,10,28,6,tzinfo=timezone.utc)))
    assert not list(tmp_path.glob('*.xlsx*'))
