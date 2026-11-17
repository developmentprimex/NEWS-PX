from app.auth import ADMIN_ID, User
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.intelligence.router import list_items, repost_watch_status
from app.intelligence.schemas import NewsSourceCreate
from app.import_repost_watch import identity


def test_excel_manifest_preserves_both_lists_without_duplicate_collectors():
    manifest = json.loads((Path(__file__).parents[2] / "data" / "repost-watch-sources.json").read_text(encoding="utf-8"))
    rows = manifest["sources"]
    assert len(rows) == 17
    assert len({identity(row["url"]) for row in rows}) == 17
    assert sum("CEO" in row["groups"] for row in rows) == 16
    assert sum("COMPANY" in row["groups"] for row in rows) == 15
    assert sum(len(row["groups"]) == 2 for row in rows) == 14
    for row in rows:
        payload = NewsSourceCreate(name=row["name"], url=row["url"], type="LINKEDIN", repost_groups=row["groups"], repost_guidance=row["guidance"])
        assert set(payload.repost_groups) == set(row["guidance"])
        assert not payload.email_enabled


def test_repost_group_requires_linkedin():
    with pytest.raises(ValueError, match="LinkedIn"):
        NewsSourceCreate(name="Website", url="https://example.org", type="WEBSITE", repost_groups=["CEO"])
    payload = NewsSourceCreate(name="Company", url="https://www.linkedin.com/company/example/", type="LINKEDIN", repost_groups=["CEO", "CEO"])
    assert payload.repost_groups == ["CEO"]


def test_group_filter_is_applied_before_feed_limit():
    statements = []
    class FakeDB:
        async def execute(self, query):
            statements.append(query)
            return SimpleNamespace(all=lambda: [])
    asyncio.run(list_items(read_state="all", due_soon=False, saved_only=False, repost_group="COMPANY", db=FakeDB(), user=User(id=ADMIN_ID, email="test@example.com")))
    query = str(statements[-1].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))
    assert "@>" in query
    assert query.index("WHERE") < query.index("LIMIT")


def test_unread_badge_counts_shared_post_once_and_in_each_group():
    rows = [(uuid4(), ["CEO"]), (uuid4(), ["CEO", "COMPANY"]), (uuid4(), ["COMPANY"])]
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
    result = asyncio.run(repost_watch_status(db=db, user=User(id=ADMIN_ID, email="test@example.com")))
    assert result == {"unread": 3, "CEO": 2, "COMPANY": 2}
