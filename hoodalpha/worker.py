"""
hoodalpha/worker.py

HoodAlpha's background service. Run with:

    python -m hoodalpha.worker

Owns two loops, each under its own Redis leader lease (hoodalpha/locks.py
-- disjoint key prefix from WhaleAlpha's own leases, same Redis
instance):

  - "hoodalpha:ingest"  -> hoodalpha/ingest.py::ingest_once()
  - "hoodalpha:monitor" -> hoodalpha/monitor.py::run_cycle()

Never imports workers/signal_trading_worker.py or any other WhaleAlpha
worker module -- this is its own process, wired up independently.
"""

from __future__ import annotations

import asyncio
import logging

from hoodalpha.locks import run_as_leader
from hoodalpha.telegram import build_bot
from hoodalpha.db import close_db
from hoodalpha.settings import HOOD_INGEST_INTERVAL_SECONDS, HOOD_MONITOR_INTERVAL_SECONDS
from hoodalpha.ingest import ingest_once
from hoodalpha.monitor import run_cycle

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("HoodAlpha.Worker")


async def _ingest_loop():
    while True:
        try:
            await ingest_once()
        except Exception:
            logger.exception("Ingest cycle failed")
        await asyncio.sleep(HOOD_INGEST_INTERVAL_SECONDS)


async def _monitor_loop(bot):
    while True:
        try:
            summary = await run_cycle(bot)
            if summary["checked"]:
                logger.info(
                    "Monitor cycle: checked=%d alerts=%d staled=%d",
                    summary["checked"], summary["alerts"], summary["staled"],
                )
        except Exception:
            logger.exception("Monitor cycle failed")
        await asyncio.sleep(HOOD_MONITOR_INTERVAL_SECONDS)


async def main() -> None:
    logger.info("HoodAlpha worker starting...")
    bot = build_bot()

    try:
        await asyncio.gather(
            run_as_leader("ingest", _ingest_loop, lease_seconds=90, renew_interval_seconds=30),
            run_as_leader("monitor", lambda: _monitor_loop(bot), lease_seconds=90, renew_interval_seconds=30),
        )
    finally:
        await bot.session.close()
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
