"""domain/trading/auto_trade/worker.py

The two background loops for the Auto-Trade Engine, in the same shape
as domain/trading/real/real_automation_engine.py::real_automation_loop
and domain/trading/real/real_exit_engine.py::real_exit_engine_loop, so
they slot into workers/signal_trading_worker.py's existing
run_as_leader(...) registration pattern without needing any new
worker-orchestration machinery.

These are deliberately two SEPARATE leader-elected loops (own lock
keys: "loop:auto_trade_scan" / "loop:auto_trade_exit"), matching the
existing convention of splitting buy-side automation
(loop:real_automation) from exit monitoring (loop:real_exit_engine).
"""

from __future__ import annotations

import asyncio
import logging

from . import orchestrator, exit_engine, reconciliation

logger = logging.getLogger("AlphaPulse.AutoTrade.Worker")


async def auto_trade_scan_loop(bot, interval_seconds: int = 20) -> None:
    """Signal -> policy -> risk -> buy, once per tick, for every
    Auto-Trade-enabled user (§40)."""
    logger.info("[AutoTrade] scan-and-buy loop starting (interval=%ss)", interval_seconds)
    try:
        await reconciliation.run_startup_reconciliation()
    except Exception as e:
        logger.error("[AutoTrade] startup reconciliation failed: %s", e)

    while True:
        try:
            await orchestrator.scan_and_authorize(bot)
        except Exception as e:
            logger.exception("[AutoTrade] scan-and-buy loop iteration failed: %s", e)
        try:
            await reconciliation.sweep_orphaned_claims()
        except Exception as e:
            logger.exception("[AutoTrade] orphaned-claim sweep failed: %s", e)
        await asyncio.sleep(interval_seconds)


async def auto_trade_exit_loop(bot, interval_seconds: int = 20) -> None:
    """TP / SL / trailing-stop monitor + sell, once per tick, for every
    open position across all users (§40)."""
    logger.info("[AutoTrade] monitor-and-exit loop starting (interval=%ss)", interval_seconds)
    while True:
        try:
            await exit_engine.monitor_and_exit(bot)
        except Exception as e:
            logger.exception("[AutoTrade] monitor-and-exit loop iteration failed: %s", e)
        await asyncio.sleep(interval_seconds)
