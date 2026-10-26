import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.intelligence import collection
from app.intelligence.services import CollectedNewsItem


@pytest.mark.parametrize("subscribed,expected", [(True, True), (False, False)])
def test_email_subscription_keeps_activity_outside_category_filter(monkeypatch, subscribed, expected):
    source = SimpleNamespace(id=uuid4(), type="RSS", url="https://example.com/feed", categories=["GRANTS"])
    stored = []
    db = SimpleNamespace(scalar=AsyncMock(side_effect=[None, source.id if subscribed else None]),
                         scalars=AsyncMock(return_value=[]),
                         add=stored.append, flush=AsyncMock())
    analysis = SimpleNamespace(summary="News", category="BUSINESS", importanceScore=10, relevanceScore=10,
                               whyItMatters=None, deadline=None, fundingAmount=None, eligibility=None,
                               opportunityType=None, tags=[])
    monkeypatch.setattr(collection, "analyze_news_item", AsyncMock(return_value=analysis))
    post = CollectedNewsItem("email-news", "https://example.com/news", "News", "Body", None)
    assert asyncio.run(collection._store_post(db, source, post)) is expected
    assert len(stored) == (2 if subscribed else 0)


def test_engagement_refresh_keeps_existing_item_and_skips_ai(monkeypatch):
    item = SimpleNamespace(id=uuid4(), title="Original", original_text="Body", published_at=None, linkedin_data=None)
    db = SimpleNamespace(scalar=AsyncMock(return_value=item), get=AsyncMock(), add=lambda _: (_ for _ in ()).throw(AssertionError("duplicate")))
    analyze = AsyncMock()
    monkeypatch.setattr(collection, "analyze_news_item", analyze)
    post = CollectedNewsItem("42", "https://www.linkedin.com/posts/example_42", "Original", "Body", None, linkedin_data={"reactionCount": 7, "comments": []})
    assert asyncio.run(collection._store_post(db, SimpleNamespace(id=uuid4(), type="LINKEDIN"), post)) is False
    assert item.linkedin_data["reactionCount"] == 7
    assert item.linkedin_data["checkedAt"]
    analyze.assert_not_awaited()
    db.get.assert_not_awaited()


def test_edited_post_updates_same_item_and_analysis(monkeypatch):
    item_id = uuid4()
    item = SimpleNamespace(id=item_id, title="Original", original_text="Body", published_at=None, linkedin_data=None)
    saved_analysis = SimpleNamespace()
    db = SimpleNamespace(scalar=AsyncMock(return_value=item), get=AsyncMock(return_value=saved_analysis))
    analysis = SimpleNamespace(summary="Edited summary", category="BUSINESS", importanceScore=50, relevanceScore=60,
                               whyItMatters=None, deadline=None, fundingAmount=None, eligibility=None, opportunityType=None, tags=[])
    monkeypatch.setattr(collection, "analyze_news_item", AsyncMock(return_value=analysis))
    post = CollectedNewsItem("42", "https://www.linkedin.com/posts/example_42", "Edited", "Edited body", None, linkedin_data={"reactionCount": 8})
    assert asyncio.run(collection._store_post(db, SimpleNamespace(id=uuid4(), type="LINKEDIN"), post)) is False
    assert item.id == item_id
    assert item.title == "Edited" and item.original_text == "Edited body"
    assert saved_analysis.summary == "Edited summary"
    assert saved_analysis.analyzed_at


def test_sparse_refresh_keeps_content_and_previously_observed_activity(monkeypatch):
    previous = {"kind": "REPOST", "reactionCount": 7, "commentCount": 3,
                "originalPostText": "Shared news", "comments": [{"text": "Useful"}]}
    item = SimpleNamespace(id=uuid4(), title="Original", original_text="Body", published_at=None,
                           linkedin_data=previous)
    db = SimpleNamespace(scalar=AsyncMock(return_value=item), get=AsyncMock())
    analyze = AsyncMock()
    monkeypatch.setattr(collection, "analyze_news_item", analyze)
    post = CollectedNewsItem("42", "https://linkedin.com/posts/example_activity-42-x", "Post by Example", None, None,
                             linkedin_data={"kind": "POST", "reactionCount": None, "commentCount": 0,
                                            "originalPostText": None, "comments": []})
    assert asyncio.run(collection._store_post(db, SimpleNamespace(id=uuid4(), type="LINKEDIN"), post)) is False
    assert item.title == "Original" and item.original_text == "Body"
    assert item.linkedin_data["kind"] == "REPOST"
    assert item.linkedin_data["reactionCount"] == 7
    assert item.linkedin_data["commentCount"] == 0
    assert item.linkedin_data["comments"] == []
    assert item.linkedin_data["originalPostText"] == "Shared news"
    analyze.assert_not_awaited()


def test_missing_comment_preview_does_not_erase_observed_comments(monkeypatch):
    item = SimpleNamespace(id=uuid4(), title="Original", original_text="Body", published_at=None,
                           linkedin_data={"comments": [{"text": "Useful"}]})
    db = SimpleNamespace(scalar=AsyncMock(return_value=item))
    post = CollectedNewsItem("42", "https://linkedin.com/posts/example_activity-42-x", "Original", "Body", None,
                             linkedin_data={"comments": [], "commentCount": None})
    asyncio.run(collection._store_post(db, SimpleNamespace(id=uuid4(), type="LINKEDIN"), post))
    assert item.linkedin_data["comments"] == [{"text": "Useful"}]
