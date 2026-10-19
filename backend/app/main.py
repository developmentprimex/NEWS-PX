from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text

from app.auth import router as auth_router
from app.config import settings
from app.db import AppMetadata, Base, SessionLocal, engine
from app.import_primeflow import import_export
from app.intelligence import models  # noqa: F401 - registers isolated tables
from app.intelligence import collection_cycles  # noqa: F401 - additive trial tables
from app.intelligence.collection import check_due_sources
from app.intelligence.credit_migration import migrate_credit_controls
from app.intelligence.company_sources import ensure_company_sources, ensure_personal_activity_source
from app.intelligence.digest_service import run_digest_cycle
from app.intelligence.cycle_service import run_collection_trial_cycle
from app.intelligence.router import router as intelligence_router
from app.intelligence.digest_router import router as digest_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("ALTER TABLE intelligence_items ADD COLUMN IF NOT EXISTS linkedin_data JSONB"))
        await connection.execute(text("ALTER TABLE intelligence_sources ADD COLUMN IF NOT EXISTS repost_groups JSONB NOT NULL DEFAULT '[]'::jsonb"))
        await connection.execute(text("ALTER TABLE intelligence_sources ADD COLUMN IF NOT EXISTS repost_guidance JSONB NOT NULL DEFAULT '{}'::jsonb"))
        await migrate_credit_controls(connection)
    snapshot = Path(__file__).resolve().parents[2] / "data" / "primeflow-export.json"
    if settings.NEWS_AUTO_IMPORT_SNAPSHOT and snapshot.is_file():
        async with SessionLocal() as session:
            imported = await session.scalar(select(AppMetadata.key).where(AppMetadata.key == "primeflow_snapshot_imported"))
        if not imported:
            await import_export(snapshot)
    async with SessionLocal() as session:
        await ensure_company_sources(session)
        await ensure_personal_activity_source(session)
    scheduler = AsyncIOScheduler(timezone="UTC")
    if settings.NEWS_ENABLE_COLLECTION:
        scheduler.add_job(check_due_sources, "interval", minutes=5, id="source-collection", coalesce=True,
                          misfire_grace_time=60, max_instances=1)
    scheduler.add_job(run_digest_cycle, "interval", minutes=1, next_run_time=datetime.now(timezone.utc), max_instances=1)
    scheduler.add_job(run_collection_trial_cycle, "interval", minutes=1, id='collection-trial',
                      next_run_time=datetime.now(timezone.utc), coalesce=True, max_instances=1)
    scheduler.start()
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await engine.dispose()


app = FastAPI(title="News Intelligence API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.NEWS_PUBLIC_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(intelligence_router, prefix="/api/intelligence", tags=["intelligence"])
app.include_router(digest_router, prefix="/api/intelligence", tags=["email"])


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
