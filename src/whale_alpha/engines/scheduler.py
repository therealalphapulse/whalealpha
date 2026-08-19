"""Periodic signal evaluation — port of src/engines/monitor/scheduler.ts.

Periodically scans the recent-event buffer for tokens with clustered whale
accumulation and, if a candidate clears the confidence threshold, persists a
Signal, notifies subscribed users, and hands it to auto-trading processing.
Kept intentionally simple (asyncio task + sleep loop, same as the original's
setInterval) — swap for a proper scheduled job (arq cron / celery beat) once
running multiple workers.

Library-driven difference (see PORTING_NOTES.md): grammY/Node used
`setInterval` returning a cancel function; asyncio has no direct equivalent,
so `start_scheduler` spawns an `asyncio.Task` and returns a `stop()` coroutine
that cancels it — same call-site shape (`stop = start_scheduler(...)`, later
`await stop()`).

--- NEW vs. the original TS port (closes both scheduler TODOs) ---
`_evaluate_all_tokens` now:
  1. Fetches a live SOL/USD price and, best-effort, the token's own USD price
     (integrations/price_feed.py) to fill in `entry_zone_low/high` on the
     Signal — previously always None, "filled in by caller" per
     engines/signal.py's comment, but nothing ever filled it in.
  2. Calls `services.notification.notify_signal_subscribers` so the signal
     actually reaches users instead of sitting in Postgres.
  3. Builds the eligible-user list (`engines.auto_trading.build_eligible_users`)
     and calls `process_signal_for_auto_trading` for them.
Both steps are best-effort and independently wrapped so a notification
failure can't block auto-trading and vice versa.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from aiogram import Bot
from solana.rpc.async_api import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from whale_alpha.config import Env
from whale_alpha.db.models import Signal
from whale_alpha.engines.ai_insight import enrich_signal
from whale_alpha.engines.auto_trading import build_eligible_users, process_signal_for_auto_trading
from whale_alpha.engines.monitor import event_buffer
from whale_alpha.engines.signal import SignalEngineConfig, evaluate_token_cluster
from whale_alpha.integrations import price_feed
from whale_alpha.services.notification import notify_signal_subscribers
from whale_alpha.utils.logger import child_logger

log = child_logger("scheduler")

EVALUATION_INTERVAL_SECONDS = 30


def start_scheduler(
    env: Env,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    http_client: httpx.AsyncClient,
    solana_connection: AsyncClient,
) -> Callable[[], Awaitable[None]]:
    async def _loop() -> None:
        while True:
            await asyncio.sleep(EVALUATION_INTERVAL_SECONDS)
            try:
                await _evaluate_all_tokens(env, session_factory, bot, http_client, solana_connection)
            except Exception as err:  # noqa: BLE001 — mirrors the TS catch-all
                log.error("Signal evaluation cycle failed", err=str(err))

    task = asyncio.create_task(_loop())

    async def stop() -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return stop


async def _evaluate_all_tokens(
    env: Env,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    http_client: httpx.AsyncClient,
    solana_connection: AsyncClient,
) -> None:
    events = event_buffer.recent()
    token_mints = {e.token_mint for e in events}

    config = SignalEngineConfig(
        min_wallets=env.SIGNAL_MIN_WALLETS,
        window_minutes=env.SIGNAL_WINDOW_MINUTES,
        min_confidence=env.SIGNAL_MIN_CONFIDENCE,
    )

    # Shared across every token this cycle — one HTTP round trip for SOL/USD
    # rather than one per candidate.
    sol_price_usd = await price_feed.get_sol_price_usd(http_client, env)
    if sol_price_usd is None:
        log.warning("SOL/USD price unavailable this cycle — auto-trading will be skipped")

    for token_mint in token_mints:
        # TODO(integration): fetch real safety context (liquidity, holder
        # concentration, LP lock, mint/freeze authority) from your
        # price/liquidity feed before evaluating, and pass it instead of None.
        # Signals without a safety context are scored more conservatively (see
        # engines/signal.py safety_component) — carried over verbatim.
        candidate = evaluate_token_cluster(token_mint, events, None, config)
        if candidate is None:
            continue

        token_price_usd = await price_feed.get_price_usd(http_client, env, token_mint)

        async with session_factory() as session:
            cutoff = datetime.now(UTC) - timedelta(minutes=config.window_minutes)
            existing_result = await session.execute(
                select(Signal).where(Signal.token_mint == token_mint, Signal.created_at >= cutoff)
            )
            if existing_result.scalar_one_or_none() is not None:
                continue  # avoid duplicate signals for the same cluster

            # Best-effort: rewrite the templated ai_recommendation with a
            # Claude-written explanation grounded in this candidate's actual
            # numbers. Never raises — falls back to the template on any
            # failure (see engines/ai_insight.py docstring).
            candidate.ai_recommendation = await enrich_signal(env, candidate)

            entry_zone_low = entry_zone_high = None
            if token_price_usd is not None:
                # +/-2% band around the current price — a simple, documented
                # heuristic (not present in the original, which left this
                # None). Tune per your own entry-timing model if you have one.
                entry_zone_low = token_price_usd * 0.98
                entry_zone_high = token_price_usd * 1.02

            signal = Signal(
                token_mint=candidate.token_mint,
                wallet_count=candidate.wallet_count,
                total_capital_usd=candidate.total_capital_usd,
                confidence_score=candidate.confidence_score,
                risk_level=candidate.risk_level,
                ai_recommendation=candidate.ai_recommendation,
                entry_zone_low=entry_zone_low,
                entry_zone_high=entry_zone_high,
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

            try:
                await notify_signal_subscribers(bot, session, signal, candidate)
            except Exception as err:  # noqa: BLE001
                log.error("Signal notification pass failed", signal_id=signal.id, err=str(err))

            if sol_price_usd is not None:
                try:
                    eligible_users = await build_eligible_users(session, solana_connection, sol_price_usd)
                    if eligible_users:
                        outcomes = await process_signal_for_auto_trading(
                            session,
                            http_client,
                            env,
                            candidate,
                            signal.id,
                            market_cap_usd=signal.market_cap_usd,
                            liquidity_usd=signal.liquidity_usd,
                            users=eligible_users,
                            sol_price_usd=sol_price_usd,
                        )
                        log.info(
                            "Auto-trading pass complete",
                            signal_id=signal.id,
                            eligible=len(eligible_users),
                            executed=sum(1 for o in outcomes if o.approved),
                        )
                except Exception as err:  # noqa: BLE001
                    log.error("Auto-trading pass failed", signal_id=signal.id, err=str(err))
