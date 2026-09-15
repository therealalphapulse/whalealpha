"""
workers/signal_worker.py

Signal Worker (Bible §11 follow-up). Run with:
    python -m workers.signal_worker

The signal-only half of the split whose job lists live in
workers/signal_trading_worker.py as build_signal_alert_jobs() /
build_trading_jobs() -- see that module's docstrings for why the split
exists and why both new worker entrypoints import it rather than
duplicating the job lists or the run_as_leader wiring.

Owns: signal discovery, scoring, filtering, and alert generation
(PumpRadar / Wallet Consensus / Robinhood Discovery token discovery,
the alert engine, signal lifecycle tracking, scheduled broadcasts,
paper-trading monitor, payment-expiry sweep). Never touches trade
execution or position management -- see workers/trading_worker.py for
that half.

Shares the same Postgres database and Redis instance as the Trading
Worker. The two never call into each other in-process; they only
communicate through:
  - Postgres: this worker writes signal_tokens / alerts / paper_* rows
    that the Trading Worker's loops read (e.g. auto_trade_scan_loop
    selecting qualifying signals to act on).
  - Redis: each background loop here takes its own leader lease via
    infra.locks.run_as_leader before running a cycle, so multiple
    replicas of this worker can never double-run the same loop. Those
    lease keys ("loop:alert_engine", "loop:pump_radar", etc.) are
    disjoint from the Trading Worker's ("loop:real_dca",
    "loop:auto_trade_scan", etc.), so the two workers never contend
    for the same lock.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config.settings import BOT_TOKEN
from infra.observability.logging_config import configure_logging
from infra.observability.metrics import configure_metrics
from infra.observability.error_tracking import configure_error_tracking
from infra.db.session import close_db
from domain.signals.keyboard_provider import set_keyboard_factory

# Importing workers.signal_trading_worker (rather than re-implementing
# its contents) triggers its module-level holder-state / discovery
# adapter installs exactly once for this process, and gives us
# build_signal_alert_jobs() without duplicating the job list or the
# run_as_leader wiring -- see that module's top-of-file comments for
# why those installs are explicit rather than import-hook-timed.
import workers.signal_trading_worker as signal_trading_worker

logger = logging.getLogger("AlphaPulse.Worker.Signal")


async def main() -> None:
    configure_logging()
    configure_error_tracking()
    configure_metrics(port=9091)

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    from app_platform.keyboards.token_actions import token_actions_keyboard
    set_keyboard_factory(token_actions_keyboard)

    logger.info("Signal worker starting (signal discovery / scoring / filtering / alerts only)...")

    jobs = signal_trading_worker.build_signal_alert_jobs(bot)

    try:
        await asyncio.gather(*jobs)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
