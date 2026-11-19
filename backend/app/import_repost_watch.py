"""Import the two curated repost lists without duplicating monitored accounts."""
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from sqlalchemy import select

from app.db import SessionLocal
from app.auth import ADMIN_ID
from app.intelligence.models import NewsSource
from app.intelligence.schemas import NewsSourceCreate


def identity(url: str) -> str:
    return unquote(urlparse(url).path).strip("/").casefold()


async def import_sources(path: Path) -> None:
    rows = json.loads(path.read_text(encoding="utf-8"))["sources"]
    created = updated = 0
    async with SessionLocal() as db:
        existing = {identity(source.url): source for source in (await db.scalars(
            select(NewsSource).where(NewsSource.type == "LINKEDIN", NewsSource.owner_id == ADMIN_ID)
        )).all()}
        for row in rows:
            payload = NewsSourceCreate(
                name=row["name"], url=row["url"], type="LINKEDIN",
                repost_groups=row["groups"], repost_guidance=row["guidance"],
                ai_instructions="Summarize this public LinkedIn post for manual repost review by PrimEx. Describe the actual post content; do not invent a recommendation to publish.",
            )
            key = identity(payload.url)
            source = existing.get(key)
            if source is None:
                source = NewsSource(**payload.model_dump(exclude={"email_enabled"}))
                db.add(source)
                existing[key] = source
                created += 1
            else:
                source.repost_groups = list(dict.fromkeys([*(source.repost_groups or []), *payload.repost_groups]))
                source.repost_guidance = {**(source.repost_guidance or {}), **payload.repost_guidance}
                updated += 1
        await db.commit()
    print(json.dumps({"created": created, "updated": updated, "CEO": sum("CEO" in row["groups"] for row in rows),
                      "COMPANY": sum("COMPANY" in row["groups"] for row in rows)}))


if __name__ == "__main__":
    asyncio.run(import_sources(Path(sys.argv[1])))
