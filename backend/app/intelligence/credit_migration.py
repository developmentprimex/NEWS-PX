"""Additive migration; existing posts, analyses and reading state are retained."""
from sqlalchemy import text

SOURCE_COLUMNS = {
    "last_successful_fetch_at": "TIMESTAMPTZ",
    "pending_execution_id": "UUID",
    "empty_fetch_streak": "INTEGER NOT NULL DEFAULT 0",
    "last_new_posts": "INTEGER NOT NULL DEFAULT 0",
    "linkedin_paused_reason": "VARCHAR(500)",
}


async def migrate_credit_controls(connection):
    # Additive ownership registry: historical duplicate sources/items are retained.
    await connection.execute(text("""CREATE TABLE IF NOT EXISTS intelligence_linkedin_source_identities (
        canonical_url VARCHAR(2000) PRIMARY KEY,
        source_id UUID NOT NULL UNIQUE REFERENCES intelligence_sources(id) ON DELETE CASCADE)"""))
    for name, definition in SOURCE_COLUMNS.items():
        await connection.execute(text(f"ALTER TABLE intelligence_sources ADD COLUMN IF NOT EXISTS {name} {definition}"))
    # Safe on every restart and covers sources inserted by older import tools.
    await connection.execute(text("UPDATE intelligence_sources SET fetch_interval_minutes = 1440 WHERE type = 'LINKEDIN' AND fetch_interval_minutes < 1440"))
    await connection.execute(text("UPDATE intelligence_sources SET last_successful_fetch_at = last_checked_at WHERE type = 'LINKEDIN' AND last_successful_fetch_at IS NULL AND last_checked_at IS NOT NULL"))
