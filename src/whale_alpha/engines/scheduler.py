"""Periodic signal evaluation — port of src/engines/monitor/scheduler.ts.

Periodically scans the recent-event buffer for tokens with clustered whale
accumulation and, if a candidate clears the confidence threshold, persists a
Signal and hands it to auto-trading processing. Kept intentionally simple
(asyncio task + sleep loop, same as the original's setInterval) — swap for a
proper scheduled job (arq cron / celery beat) once running multiple workers.

Library-driven difference (see PORTING_NOTES.md): grammY/Node used
`setInterval` returning a cancel function; asyncio has no direct equivalent,
so `start_scheduler` spawns an `asyncio.Task` and returns a `stop()` coroutine
that cancels it — same call-site shape (`stop = start_scheduler(...)`, later
`await stop()`).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from whale_alpha.config import Env
from whale_alpha.db.models import Signal
from whale_alpha.engines.monitor import event_buffer
from whale_alpha.engines.signal import SignalEngineConfig, evaluate_token_cluster
from whale_alpha.utils.logger import child_logger

log = child_logger("scheduler")

EVALUATION_INTERVAL_SECONDS = 30


def start_scheduler(env: Env, session_factory: async_sessionmaker):
    async def _loop() -> None:
        while True:
            await asyncio.sleep(EVALUATION_INTERVAL_SECONDS)
            try:
                await _evaluate_all_tokens(env, session_factory)
            except Exception as err:  # noqa: BLE001 — mirrors the TS catch-all
                log.error("Signal evaluation cycle failed", err=str(err))

    task = asyncio.create_task(_loop())

    async def stop() -> None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return stop


async def _evaluate_all_tokens(env: Env, session_factory: async_sessionmaker) -> None:
    events = event_buffer.recent()
    token_mints = {e.token_mint for e in events}

    config = SignalEngineConfig(
        min_wallets=env.SIGNAL_MIN_WALLETS,
        window_minutes=env.SIGNAL_WINDOW_MINUTES,
        min_confidence=env.SIGNAL_MIN_CONFIDENCE,
    )

    for token_mint in token_mints:
        # TODO(integration): fetch real safety context (liquidity, holder
        # concentration, LP lock, mint/freeze authority) from your
        # price/liquidity feed before evaluating, and pass it instead of None.
        # Signals without a safety context are scored more conservatively (see
        # engines/signal.py safety_component) — carried over verbatim.
        candidate = evaluate_token_cluster(token_mint, events, None, config)
        if candidate is None:
            continue

        async with session_factory() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=config.window_minutes)
            existing_result = await session.execute(
                select(Signal).where(Signal.token_mint == token_mint, Signal.created_at >= cutoff)
            )
            if existing_result.scalar_one_or_none() is not None:
                continue  # avoid duplicate signals for the same cluster

            signal = Signal(
                token_mint=candidate.token_mint,
                wallet_count=candidate.wallet_count,
                total_capital_usd=candidate.total_capital_usd,
                confidence_score=candidate.confidence_score,
                risk_level=candidate.risk_level,
                ai_recommendation=candidate.ai_recommendation,
            )
            session.add(signal)
            await session.commit()
            await session.refresh(signal)

            log.info(
                "Signal generated",
                signal_id=signal.id,
                token_mint=token_mint,
                confidence=candidate.confidence_score,
            )

        # TODO(integration): notify subscribed users (services/notification)
        # and call engines.auto_trading.process_signal_for_auto_trading for
        # eligible users — carried over verbatim from the original.
