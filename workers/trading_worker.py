"""
workers/trading_worker.py

Trading Worker (Bible §11 follow-up). Run with:
    python -m workers.trading_worker

The trading-only half of the split whose job lists live in
workers/signal_trading_worker.py as build_signal_alert_jobs() /
build_trading_jobs() -- see that module's docstrings for why the split
exists and why both new worker entrypoints import it rather than
duplicating the job lists or the run_as_leader wiring.

Owns: trade execution and position management (real DCA scheduler,
real exit engine, real limit orders, auto-trade scan/exit, and --
when REAL_AUTOMATION_ENABLED -- real automation). Never runs signal
discovery, scoring, filtering, or alert generation -- see
workers/signal_worker.py for that half.

Shares the same Postgres database and Redis instance as the Signal
Worker. The two never call into each other in-process; they only
communicate through:
  - Postgres: this worker reads the signal_tokens / alerts rows the
    Signal Worker produces (e.g. auto_trade_scan_loop selecting
    qualifying, alert_delivered signals to act on) and writes its own
    auto_trade_* / real_* trade and position rows.
  - Redis: each background loop here takes its own leader lease via
    infra.locks.run_as_leader before running a cycle, so multiple
    replicas of this worker can never double-run the same loop --
    critical here specifically, since a double-run of a trading loop
    means double-executing a real trade, not just wasted work. Those
    lease keys ("loop:real_dca", "loop:auto_trade_scan", etc.) are
    disjoint from the Signal Worker's ("loop:alert_engine",
    "loop:pump_radar", etc.), so the two workers never contend for the
    same lock.
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
from domain.trading.real.robinhood_wallet import migrate_real_wallet_schema

# Importing workers.signal_trading_worker (rather than re-implementing
# its contents) triggers its module-level holder-state / discovery
# adapter installs exactly once for this process, and gives us
# build_trading_jobs() without duplicating the job list or the
# run_as_leader wiring. This worker never runs the discovery loops
# themselves, but several trading-side checks (e.g. risk gates / exit
# evaluation) read the same holder-state cache the Signal Worker
# populates, so the same install sequence is needed here too -- see
# that module's top-of-file comments for why the installs are explicit
# rather than import-hook-timed.
import workers.signal_trading_worker as signal_trading_worker

logger = logging.getLogger("AlphaPulse.Worker.Trading")


async def main() -> None:
    configure_logging()
    configure_error_tracking()
    configure_metrics(port=9093)

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    from app_platform.keyboards.token_actions import token_actions_keyboard
    set_keyboard_factory(token_actions_keyboard)

    try:
        await migrate_real_wallet_schema()
    except Exception as e:
        logger.error(f"RealWallet schema migration failed at worker startup: {e}")

    logger.info("Trading worker starting (trade execution / position management only)...")

    jobs = signal_trading_worker.build_trading_jobs(bot)

    try:
        await asyncio.gather(*jobs)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
