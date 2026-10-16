"""Rehearse migration on a restored PRIVATE database; never target production.

Start your private cluster on 127.0.0.1:55440 with role credit_rehearsal,
restore the production backup there, and set NEWS_DATABASE_URL accordingly.
This command performs DDL ONLY on that explicitly constrained private target.
"""
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

candidate = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(candidate / "backend"))
url = make_url(os.environ.get("NEWS_DATABASE_URL", "postgresql+asyncpg://invalid@invalid/invalid"))
if (url.host, url.port, url.username, url.database) != ("127.0.0.1", 55440, "credit_rehearsal", "postgres"):
    raise SystemExit("Refusing migration: target must be the private rehearsal cluster, not production")
os.environ.update({"NEWS_ENABLE_COLLECTION": "false", "NEWS_LINKEDIN_FETCH_ENABLED": "false",
    "NEWS_LINKEDIN_HIGH_PRIORITY_EXTRA": "false", "NEWS_AUTO_IMPORT_SNAPSHOT": "false",
    "BRIGHTDATA_API_TOKEN": "", "OPENAI_API_KEY": "", "EMAIL_USER": "", "EMAIL_PASSWORD": ""})
os.environ.setdefault("NEWS_ADMIN_EMAIL", "rehearsal@example.com")
os.environ.setdefault("NEWS_ADMIN_PASSWORD", "rehearsal-only")
os.environ.setdefault("NEWS_SESSION_SECRET", "rehearsal-only")

from sqlalchemy import select, text
from app.db import Base, engine
from app.intelligence import models
from app.intelligence.credit_migration import SOURCE_COLUMNS, migrate_credit_controls


async def table_rows(connection, table):
    return [dict(row) for row in (await connection.execute(text(f'SELECT * FROM "{table}"'))).mappings()]


def fingerprint(rows):
    return hashlib.sha256("\n".join(sorted(json.dumps(row, default=str, sort_keys=True)
                                        for row in rows)).encode()).hexdigest()


async def main():
    protected = ("intelligence_items", "intelligence_analyses", "intelligence_user_states")
    async with engine.begin() as connection:
        identity = (await connection.execute(text("SELECT current_user, current_database()"))).one()
        if tuple(identity) != ("credit_rehearsal", "postgres"):
            raise RuntimeError("Unexpected database identity")
        before = {table: await table_rows(connection, table) for table in protected}
        sources = await table_rows(connection, "intelligence_sources")
        # Match API startup ordering, without importing lifespan or collectors.
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("ALTER TABLE intelligence_items ADD COLUMN IF NOT EXISTS linkedin_data JSONB"))
        await connection.execute(text("ALTER TABLE intelligence_sources ADD COLUMN IF NOT EXISTS repost_groups JSONB NOT NULL DEFAULT '[]'::jsonb"))
        await connection.execute(text("ALTER TABLE intelligence_sources ADD COLUMN IF NOT EXISTS repost_guidance JSONB NOT NULL DEFAULT '{}'::jsonb"))
        await migrate_credit_controls(connection)
        await migrate_credit_controls(connection)
        after = {table: await table_rows(connection, table) for table in protected}
        for table in protected:
            keys = set(before[table][0]) if before[table] else set()
            original_columns = [{key: row[key] for key in keys} for row in after[table]]
            assert len(before[table]) == len(after[table])
            assert fingerprint(before[table]) == fingerprint(original_columns), f"Data changed: {table}"
        old_by_id = {str(row["id"]): row for row in sources}
        migrated = await table_rows(connection, "intelligence_sources")
        assert set(old_by_id) == {str(row["id"]) for row in migrated}, "Source identities changed"
        for row in migrated:
            old = old_by_id[str(row["id"])]
            for key, value in old.items():
                if key in SOURCE_COLUMNS or key == "fetch_interval_minutes":
                    continue
                assert row[key] == value, f"Source field changed: {key}"
            expected_interval = max(1440, old["fetch_interval_minutes"]) if old["type"] == "LINKEDIN" else old["fetch_interval_minutes"]
            assert row["fetch_interval_minutes"] == expected_interval
        # Loading all sources exercises the newly added column types/defaults.
        assert len((await connection.execute(select(models.NewsSource))).all()) == len(sources)
    await engine.dispose()
    print(json.dumps({"result": "PASS", "protected_rows": {table: len(rows) for table, rows in after.items()},
        "sources_preserved": len(sources), "data_hashes_unchanged": True,
        "migration_runs": 2, "production_changed": False}, indent=2))


asyncio.run(main())
