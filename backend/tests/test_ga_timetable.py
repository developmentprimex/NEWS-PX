import asyncio
import io
import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from app.config import settings
from app.intelligence import ga_timetable as service, digest_service
from app.mail import EmailAttachment, GmailService


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


@pytest.mark.parametrize('phase', ['scheduled-pm', 'scheduled-day'])
def test_evening_and_collection_slots_never_load_binding_or_export(monkeypatch, phase):
    monkeypatch.setattr(settings, 'NEWS_GA_TIMETABLE_BINDING_FILE', 'private-binding')
    monkeypatch.setattr(service, 'load_binding', lambda: pytest.fail('Evening must not read PrimeFlow'))
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
    asyncio.run(mailer.send_verified('07:00', ['recipient@example.com'], 'News', html='<p>News</p>', attachments=[attachment]))
    assert len(messages) == 1
    parts = list(messages[0].iter_attachments())
    assert len(parts) == 1 and parts[0].get_content_type() == 'application/pdf'
    assert parts[0].get_filename() == attachment.filename
    assert parts[0].get_payload(decode=True) == attachment.data
    assert messages[0].get_body(preferencelist=('html',)).get_content().strip() == '<p>News</p>'


def test_digest_forwards_attachment_to_the_existing_transport(monkeypatch):
    send = AsyncMock()
    from types import SimpleNamespace
    monkeypatch.setattr(digest_service, 'GmailService', lambda: SimpleNamespace(host='smtp.gmail.com', port=465, send_verified=send))
    attachment = EmailAttachment('GA.pdf', pdf())
    asyncio.run(digest_service._send_digest('07:00', 'News', '<p>News</p>', attachments=[attachment]))
    assert send.await_args.kwargs['attachments'] == [attachment]
