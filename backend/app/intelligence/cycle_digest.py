"""Strict evening intervals and a durable, non-retryable SMTP claim."""
import hashlib
from html import escape
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource, CollectionCycleReport, confirmed_activity_time
from app.intelligence.cycle_service import trial_configuration, source_identity, TERMINAL
from app.intelligence.digest_service import activity_delivery_key, activity_kind, digest_content, _stamp
from app.intelligence.models import NewsItem, NewsAnalysis, NewsSource, NewsSourceEmailSubscription, NewsDigestDelivery
from app.mail import GmailService


def recipient_fingerprint(value):
    return hashlib.sha256(value.strip().casefold().encode()).hexdigest()


async def subscribed_windows(db, cycle_id, user_id):
    return list((await db.execute(select(CollectionCycleSource, NewsSourceEmailSubscription)
        .join(NewsSourceEmailSubscription, NewsSourceEmailSubscription.source_id == CollectionCycleSource.source_id)
        .where(CollectionCycleSource.cycle_id == cycle_id, NewsSourceEmailSubscription.user_id == user_id))).all())


async def trial_digest_entries(db, cycle_id, user_id):
    entries = []
    for window, subscription in await subscribed_windows(db, cycle_id, user_id):
        source = await db.get(NewsSource, window.source_id)
        if not source or source.status != 'ACTIVE' or source_identity(source) != window.canonical_url:
            continue
        rows = (await db.execute(select(NewsItem, NewsAnalysis).outerjoin(NewsAnalysis,
            NewsAnalysis.news_item_id == NewsItem.id).where(NewsItem.source_id == source.id)
            .order_by(NewsItem.published_at, NewsItem.id))).all()
        for item, analysis in rows:
            stamp = confirmed_activity_time(item)
            if not stamp or not window.window_start < stamp <= window.window_end or stamp < subscription.enabled_at:
                continue
            if item.created_at < subscription.enabled_at:
                continue
            key = activity_delivery_key(item)
            legacy = hashlib.sha256(item.url.encode()).hexdigest()
            receipts = list(await db.scalars(select(NewsDigestDelivery).where(
                NewsDigestDelivery.user_id == user_id, NewsDigestDelivery.url_hash.in_([key, legacy]))))
            if any(r.url_hash == key or (activity_kind(item, source) != 'REPOST' and r.sent_at >= item.created_at)
                   for r in receipts):
                continue
            entries.append({'item': item, 'analysis': analysis, 'source': source})
    return entries


def trial_digest_content(entries, source_windows, cutoff):
    sources, interval_lines = [], []
    for window in source_windows:
        row = window['row']
        status = None if row.state == 'completed' else 'Interval coverage is incomplete.'
        sources.append(SimpleNamespace(id=row.source_id, name=row.name, type=row.type,
            last_checked_at=row.completed_at, last_error=status,
            pending_snapshot_id='pending' if row.state not in TERMINAL else None))
        interval_lines.append(f'{row.name}: ({_stamp(row.window_start)}, {_stamp(row.window_end)}]'
            + ('' if row.verified_start else ' · Historiku para intervalit është i paverifikuar')
            + f" · Orë të paverifikuara në feed: {window.get('unknown', 0)}")
    start = min((w['row'].window_start for w in source_windows), default=cutoff)
    subject, body, html = digest_content(entries, cutoff, interval_start=start, sources=sources)
    legacy_copy = 'Aktivitetet e marra me vonesë përfshihen në raportin pasues dhe shënohen veçmas.'
    html = html.replace(legacy_copy, 'Përzgjedhja përdor vetëm orën e konfirmuar të aktivitetit brenda intervalit për burimin.')
    body += '\n\nIntervalet sipas burimit:\n' + '\n'.join(interval_lines)
    html = html.replace('</body>', '<div style="max-width:700px;margin:auto;font-family:Arial;font-size:12px">'
        + '<br>'.join(escape(line) for line in interval_lines) + '</div></body>')
    return subject, body, html


async def _send_trial_report(subject, body, html):
    from app.intelligence.email_service import _smtp_reachable
    mailer = GmailService(credentials=(settings.NEWS_TRIAL_EMAIL_USER, settings.NEWS_TRIAL_EMAIL_PASSWORD))
    if mailer.port != 465 and not await _smtp_reachable(mailer.host, mailer.port):
        if not await _smtp_reachable(mailer.host, 465):
            raise ConnectionError('SMTP submission ports unavailable')
        mailer.port = 465
    await mailer.send_verified(subject, [settings.NEWS_EMAIL_RECIPIENT], body, html=html)


async def process_trial_digest(db, cycle_id, now):
    if not settings.NEWS_TRIAL_EMAIL_USER or not settings.NEWS_TRIAL_EMAIL_PASSWORD:
        return 0
    cycle = await db.get(CollectionCycle, cycle_id)
    config = await trial_configuration(db)
    fingerprint = recipient_fingerprint(settings.NEWS_EMAIL_RECIPIENT)
    if (not cycle or cycle.phase != 'evening' or now < cycle.cutoff_at or not config.get('enabled')
            or config.get('date') != cycle.trial_date.isoformat() or config.get('recipient_fingerprint') != fingerprint):
        return 0
    user_id = UUID(config['user_id'])
    states = list(await db.scalars(select(CollectionCycleSource.state).where(CollectionCycleSource.cycle_id == cycle_id)))
    if any(state not in TERMINAL for state in states) and now < cycle.deadline_at:
        return 0
    if not await subscribed_windows(db, cycle_id, user_id):
        return 0
    await db.execute(insert(CollectionCycleReport).values(id=uuid4(), trial_date=cycle.trial_date,
        cycle_id=cycle.id, user_id=user_id, recipient_fingerprint=fingerprint, state='planned', content={})
        .on_conflict_do_nothing(index_elements=['trial_date', 'recipient_fingerprint']))
    report = await db.scalar(select(CollectionCycleReport).where(CollectionCycleReport.trial_date == cycle.trial_date,
        CollectionCycleReport.recipient_fingerprint == fingerprint).with_for_update().execution_options(populate_existing=True))
    if report.state != 'planned':
        await db.commit()
        return 0
    entries = await trial_digest_entries(db, cycle_id, user_id)
    windows = []
    for row, _ in await subscribed_windows(db, cycle_id, user_id):
        items = list(await db.scalars(select(NewsItem).where(NewsItem.source_id == row.source_id)))
        windows.append({'row': row, 'unknown': sum(confirmed_activity_time(item) is None for item in items)})
    subject, body, html = trial_digest_content(entries, windows, cycle.cutoff_at)
    report.content = {'item_ids': [str(e['item'].id) for e in entries], 'subject': subject, 'body': body, 'html': html}
    report.state, report.claimed_at = 'sending', now
    # A crash anywhere after this commit is uncertain, not permission to resend.
    await db.commit()
    try:
        await _send_trial_report(subject, body, html)
    except Exception:
        report.state, report.error = 'uncertain', 'SMTP outcome requires verification; automatic retry disabled.'
        await db.commit()
        return 0
    for entry in entries:
        await db.execute(insert(NewsDigestDelivery).values(user_id=user_id,
            url_hash=activity_delivery_key(entry['item']), sent_at=now).on_conflict_do_nothing())
    report.state, report.sent_at = 'sent', now
    await db.commit()
    return len(entries)
