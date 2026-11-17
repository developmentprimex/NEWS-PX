"""Preserve free-form AI opportunity descriptions without a legacy length limit."""
from sqlalchemy import text


async def migrate_analysis_schema(connection):
    kind = await connection.scalar(text("""
        SELECT data_type FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'intelligence_analyses'
          AND column_name = 'opportunity_type'
    """))
    if kind == 'character varying':
        await connection.execute(text(
            'ALTER TABLE intelligence_analyses ALTER COLUMN opportunity_type TYPE TEXT'
        ))
