"""A long AI label must survive migration, storage, and subsequent startup."""
import asyncio
from uuid import uuid4

from sqlalchemy import text

from app.intelligence.models import NewsAnalysis, NewsItem, NewsSource
from app.intelligence.analysis_migration import migrate_analysis_schema
from test_brightdata_postgres_integration import private_postgres, database


def test_existing_analysis_values_and_long_ai_labels_survive_idempotent_upgrade(database):
    engine, sessions = database
    async def verify():
        source_id, item_id = uuid4(), uuid4()
        async with sessions() as db:
            db.add(NewsSource(id=source_id, name='Official news', url='https://example.com/news', type='WEBSITE'))
            await db.flush()
            db.add(NewsItem(id=item_id, source_id=source_id, url='https://example.com/news/1', title='News', content_hash='migration-test'))
            await db.flush()
            db.add(NewsAnalysis(news_item_id=item_id, summary='Existing summary', category='NEWS', importance_score=2, relevance_score=3, opportunity_type='Existing label'))
            await db.commit()
        async with engine.begin() as conn:
            await conn.execute(text('ALTER TABLE intelligence_analyses ALTER COLUMN opportunity_type TYPE VARCHAR(40)'))
            await migrate_analysis_schema(conn)
            await migrate_analysis_schema(conn)
        long_label = 'Funding opportunity for cross-border digital infrastructure — mundësi financimi ' * 5
        async with sessions() as db:
            analysis = await db.get(NewsAnalysis, item_id)
            assert analysis.summary == 'Existing summary' and analysis.opportunity_type == 'Existing label'
            analysis.opportunity_type = long_label
            await db.commit()
        async with engine.begin() as conn:
            await migrate_analysis_schema(conn)
        async with sessions() as db:
            assert (await db.get(NewsAnalysis, item_id)).opportunity_type == long_label
    asyncio.run(verify())
