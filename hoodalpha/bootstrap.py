"""
hoodalpha/bootstrap.py

Run once per deploy, BEFORE hoodalpha/worker.py or hoodalpha/gateway.py
start (Railway's "release phase" equivalent to WhaleAlpha's
`release: python -m app_platform.gateway.bootstrap` -- same pattern,
own process, own target):

    python -m hoodalpha.bootstrap

Creates the `hoodalpha` Postgres schema and its tables via
`CREATE SCHEMA IF NOT EXISTS` + SQLAlchemy metadata `create_all`, both
idempotent and safe to re-run on every deploy. This is deliberately
NOT wired into WhaleAlpha's Alembic chain (infra/db/migrations/) -- it
is its own, much simpler, schema-scoped bootstrap, matching the
"nothing here touches WhaleAlpha's migration history" requirement.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from hoodalpha.db import engine, HoodBase, close_db
import hoodalpha.models  # noqa: F401 -- registers HoodWatch/HoodAlert/HoodIngestCursor on HoodBase.metadata

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HoodAlpha.Bootstrap")


async def run() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS hoodalpha"))
        await conn.run_sync(HoodBase.metadata.create_all)

        # Seed the single ingest-cursor row if it doesn't exist yet, so
        # ingest.py can always assume exactly one row is present.
        await conn.execute(
            text(
                "INSERT INTO hoodalpha.ingest_cursor (id, last_seen_signal_id) "
                "VALUES (1, 0) ON CONFLICT (id) DO NOTHING"
            )
        )

    logger.info("HoodAlpha schema/tables ready.")
    await close_db()


if __name__ == "__main__":
    asyncio.run(run())
