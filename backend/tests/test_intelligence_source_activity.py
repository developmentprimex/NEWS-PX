import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.intelligence import router as news_router


@pytest.mark.parametrize("activity_kind", [None, "POST", "REPOST"])
def test_company_feed_filters_source_and_activity_before_limit(activity_kind):
    statements = []
    source_id = uuid4()

    class FakeDB:
        async def execute(self, query):
            statements.append(query)
            return SimpleNamespace(all=lambda: [])

    asyncio.run(news_router.list_items(db=FakeDB(), user=SimpleNamespace(id=uuid4()),
                                      source_id=source_id, activity_kind=activity_kind))
    compiled = statements[-1].compile(dialect=postgresql.dialect())
    query = str(compiled)
    assert "intelligence_sources.id =" in query
    assert source_id in compiled.params.values()
    assert query.index("WHERE") < query.index("LIMIT")
    if activity_kind:
        assert "linkedin_data" in query
        assert "LINKEDIN" in compiled.params.values()
        assert "REPOST" in compiled.params.values() if activity_kind == "REPOST" else ["POST", "ARTICLE"] in compiled.params.values()


def test_combined_company_feed_applies_both_sources_before_limit():
    statements = []
    ids = [uuid4(), uuid4()]

    class FakeDB:
        async def execute(self, query):
            statements.append(query)
            return SimpleNamespace(all=lambda: [])

    asyncio.run(news_router.list_items(db=FakeDB(), user=SimpleNamespace(id=uuid4()), source_ids=ids))
    compiled = statements[-1].compile(dialect=postgresql.dialect())
    assert "intelligence_sources.id IN" in str(compiled)
    assert ids in compiled.params.values()
    assert str(compiled).index("WHERE") < str(compiled).index("LIMIT")


@pytest.mark.parametrize('saved_only',[False,True])
def test_quarantined_items_are_hidden_before_limit_but_saved_items_remain_accessible(saved_only):
    statements=[]
    class FakeDB:
        async def execute(self,query):
            statements.append(query)
            return SimpleNamespace(all=lambda:[])
    asyncio.run(news_router.list_items(db=FakeDB(),user=SimpleNamespace(id=uuid4()),saved_only=saved_only))
    query=str(statements[-1].compile(dialect=postgresql.dialect()))
    assert ('news_app_metadata' in query) is (not saved_only)



def test_linkedin_refresh_endpoints_do_not_schedule_scraping(monkeypatch):
    from fastapi import HTTPException
    from unittest.mock import AsyncMock
    source = SimpleNamespace(id=uuid4(), type="LINKEDIN", status="ACTIVE")
    db = SimpleNamespace(get=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [source]))))
    checker = AsyncMock()
    monkeypatch.setattr(news_router, "check_source", checker)
    monkeypatch.setattr(news_router, "_check_all_task", None)
    monkeypatch.setattr(news_router, "_get_source", AsyncMock(return_value=source))
    with pytest.raises(HTTPException) as error:
        asyncio.run(news_router.check_all_sources_now(db=db, _=None))
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        asyncio.run(news_router.check_source_now(source.id, db=db, _=None))
    assert error.value.status_code == 409
    checker.assert_not_awaited()
