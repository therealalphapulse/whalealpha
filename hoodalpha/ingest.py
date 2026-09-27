"""
hoodalpha/ingest.py

Reads WhaleAlpha's `public.signal_tokens` (read-only, via
hoodalpha/db.py::fetch_delivered_signals) for rows past HoodAlpha's own
cursor where `alert_delivered = true`, and seeds one `hoodalpha.watch`
row per (contract, chain) -- the set of tokens WhaleAlpha's users were
actually shown a Signal Alert for, which is the literal "historical
alert data" HoodAlpha is meant to consume.

`alert_delivered = true` is the gate, NOT `status == 'active'`:
signal_tokens.status is set to "active" at row-creation time, before
delivery is even attempted (see models/signal_token.py's own
comments), so it is not proof any user ever saw an alert. Seeding from
it would let HoodAlpha track tokens no one was ever actually alerted
on -- the exact bug class WhaleAlpha's own lifecycle loop was fixed to
avoid.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update

from hoodalpha.db import hood_session, fetch_delivered_signals
from hoodalpha.models import HoodWatch, HoodIngestCursor
from hoodalpha.settings import HOOD_INGEST_BATCH_SIZE

logger = logging.getLogger("HoodAlpha.Ingest")


async def _get_cursor() -> int:
    async with hood_session() as session:
        row = await session.get(HoodIngestCursor, 1)
        return row.last_seen_signal_id if row else 0


async def _set_cursor(last_seen_signal_id: int) -> None:
    async with hood_session() as session:
        await session.execute(
            update(HoodIngestCursor)
            .where(HoodIngestCursor.id == 1)
            .values(last_seen_signal_id=last_seen_signal_id)
        )
        await session.commit()


async def ingest_once() -> int:
    """Runs a single ingest pass. Returns the number of new watches
    created (for logging/metrics)."""
    cursor = await _get_cursor()
    rows = await fetch_delivered_signals(after_id=cursor, limit=HOOD_INGEST_BATCH_SIZE)
    if not rows:
        return 0

    created = 0
    max_id_seen = cursor

    for row in rows:
        max_id_seen = max(max_id_seen, row["id"])

        contract = row["contract"]
        chain = row.get("chain") or "solana"
        entry_price = row.get("entry_price")
        if not entry_price or entry_price <= 0:
            # Can't track a meaningful dip/rise without a reference price.
            continue

        try:
            async with hood_session() as session:
                existing = await session.execute(
                    select(HoodWatch.id).where(
                        HoodWatch.contract == contract, HoodWatch.chain == chain
                    )
                )
                if existing.scalar_one_or_none() is not None:
                    continue  # already tracking this (contract, chain)

                watch = HoodWatch(
                    contract=contract,
                    chain=chain,
                    source_signal_id=row["id"],
                    source_symbol=row.get("symbol"),
                    source_name=row.get("name"),
                    entry_alert_price=entry_price,
                    lowest_dip_price=entry_price,
                    last_price=row.get("current_price") or entry_price,
                    status="watching",
                )
                session.add(watch)
                await session.commit()
                created += 1
        except Exception as e:
            # Isolated per-row, matching WhaleAlpha's own per-signal
            # error isolation in signal_tracker.py -- one bad row must
            # never block the rest of the ingest batch.
            logger.error("Ingest error for signal id=%s contract=%s: %s", row.get("id"), contract, e)

    await _set_cursor(max_id_seen)
    if created:
        logger.info("Ingested %d new watch(es) (cursor now %d)", created, max_id_seen)
    return created
