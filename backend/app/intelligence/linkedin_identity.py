"""Canonical monitoring identities; historical duplicate source rows are retained."""
import hashlib
import re
from urllib.parse import quote, unquote, urlsplit

from sqlalchemy import select, text

from app.intelligence.models import LinkedInSourceIdentity, NewsSource


def canonical_linkedin_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'linkedin.com', 'www.linkedin.com'}
            or parsed.username or parsed.password or parsed.port is not None):
        raise ValueError('Use a LinkedIn profile or company page URL without credentials or a custom port.')
    path = unquote(parsed.path).strip('/').split('/')
    # The activity tab is an unambiguous alias of the same personal profile.
    if len(path) in {3, 4} and path[0] == 'in' and path[2:] in [['recent-activity'], ['recent-activity', 'all']]:
        path = path[:2]
    if len(path) != 2 or path[0] not in {'in', 'company'} or not re.fullmatch(r'[\w&.-]+', path[1], re.UNICODE):
        raise ValueError('Use a LinkedIn profile or company page URL (linkedin.com/in/... or linkedin.com/company/...).')
    return f'https://www.linkedin.com/{path[0]}/{quote(path[1].casefold(), safe="-._~")}/'


def identity_or_none(value: str) -> str | None:
    try:
        return canonical_linkedin_url(value)
    except (ValueError, TypeError):
        return None


def lock_key(identity: str) -> int:
    return int.from_bytes(hashlib.sha256(f'news-linkedin:{identity}'.encode()).digest()[:8], 'big', signed=True)


async def lock_source_identity(db, url):
    identity = canonical_linkedin_url(url)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key(identity)})
    return identity


async def lock_identity_change(db, old_url, new_url):
    keys = {lock_key(identity) for value in (old_url, new_url) if (identity := identity_or_none(value))}
    for key in sorted(keys):
        await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})


async def matching_sources(db, identity):
    rows = await db.scalars(select(NewsSource).where(NewsSource.type == 'LINKEDIN')
                            .order_by(NewsSource.created_at, NewsSource.id))
    return [row for row in rows if identity_or_none(row.url) == identity]


async def collection_owner(db, source):
    """Called while holding the canonical session lock across provider commits."""
    identity = identity_or_none(source.url)
    if not identity:
        return False
    owner = await db.get(LinkedInSourceIdentity, identity)
    if owner is None:
        candidates = await matching_sources(db, identity)
        if not candidates:
            return False
        owner = LinkedInSourceIdentity(canonical_url=identity, source_id=candidates[0].id)
        db.add(owner)
        await db.flush()
    return owner.source_id == source.id
