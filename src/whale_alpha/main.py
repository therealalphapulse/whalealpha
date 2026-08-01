"""Main entrypoint — port of src/index.ts.

--- NEW vs. the TS original (porting requirement #3) ---
Before the bot starts polling Telegram or the scheduler starts evaluating
signals, we run `reconcile_pending_trades` once against the database + a
Solana RPC connection, so any trade left mid-flight by a prior process
(crash, redeploy, OOM-kill) is resolved before normal operation resumes. This
did not exist in the TS version.

--- NEW: wires up the previously-missing pieces ---
In addition to the bot's long-polling task and the signal-evaluation
scheduler, this now also starts:
  * the Helius webhook server (integrations/helius_webhook.py) — the
    previously-nonexistent inbound path for whale wallet tracking, and
  * the price-alert loop (engines/price_alerts.py) — the previously
    nonexistent % price-move alert feature.
Both are started/stopped the same way as the scheduler: a background asyncio
task plus a `stop()`/`cleanup()` call on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal as signal_module

import httpx
from redis.asyncio import Redis

from whale_alpha.bot import create_bot
from whale_alpha.config import get_env
from whale_alpha.db.session import create_engine, create_session_factory
from whale_alpha.engines.price_alerts import start_price_alert_loop
from whale_alpha.engines.reconciliation import reconcile_pending_trades
from whale_alpha.engines.scheduler import start_scheduler
from whale_alpha.integrations.helius_webhook import start_webhook_server
from whale_alpha.integrations.solana_connection import create_connection
from whale_alpha.utils.logger import child_logger, configure_logging

log = child_logger("main")


async def main() -> None:
    env = get_env()
    configure_logging(env.LOG_LEVEL, env.NODE_ENV)

    engine = create_engine(env)
    session_factory = create_session_factory(engine)
    log.info("Database connected")

    redis = Redis.from_url(env.REDIS_URL)

    reconciliation_connection = create_connection(env)
    try:
        async with session_factory() as session:
            summary = await reconcile_pending_trades(session, reconciliation_connection)
        log.info("Startup reconciliation complete", **summary)
    finally:
        await reconciliation_connection.close()

    http_client = httpx.AsyncClient(timeout=30.0)

    # Long-lived connection used by the scheduler (auto-trading eligibility /
    # balance checks) — distinct from the short-lived one reconciliation just
    # used, and distinct from the one each trade execution opens for itself
    # in engines/trade_executor.py.
    solana_connection = create_connection(env)

    bot, dp = create_bot(env, redis, session_factory, http_client)

    stop_scheduler = start_scheduler(env, session_factory, bot, http_client, solana_connection)
    stop_price_alerts = start_price_alert_loop(env, session_factory, bot, http_client)
    webhook_runner = await start_webhook_server(env, session_factory, http_client)

    stop_event = asyncio.Event()

    def _handle_sigint() -> None:
        log.info("Shutting down...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        # add_signal_handler isn't available on some platforms (e.g. Windows);
        # Railway's runtime is Linux, so this is a defensive fallback only.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_sigint)

    polling_task = asyncio.create_task(dp.start_polling(bot))
    log.info("Whale Alpha bot started", env=env.NODE_ENV)

    await stop_event.wait()

    polling_task.cancel()
    await stop_scheduler()
    await stop_price_alerts()
    await webhook_runner.cleanup()
    await solana_connection.close()
    await http_client.aclose()
    await redis.aclose()
    await engine.dispose()
    with contextlib.suppress(asyncio.CancelledError):
        await polling_task


def run() -> None:
    try:
        asyncio.run(main())
    except Exception as err:  # noqa: BLE001 — mirrors the TS catch-all in main().catch(...)
        log.error("Fatal startup error", err=str(err))
        raise SystemExit(1) from err


if __name__ == "__main__":
    run()
