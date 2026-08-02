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
    previously-nonexistent inbound path for whale wallet tracking,
  * the price-alert loop (engines/price_alerts.py) — the previously
    nonexistent % price-move alert feature, and
  * the Whale Wallet Discovery & Intelligence Engine
    (engines/discovery.py) — the previously-nonexistent automated
    wallet-sourcing/removal loop; before this, whale_wallets only ever grew
    via a human admin's /addwhale.
All three are started/stopped the same way as the scheduler: a background
asyncio task plus a `stop()`/`cleanup()` call on shutdown.

--- NEW: production-grade staged startup logging + fail-loud error handling ---
Every stage of startup now logs explicitly (env -> Postgres -> Redis ->
reconciliation -> bot -> Solana monitor -> Helius webhook -> background
workers -> ready), so a Railway deploy log always shows exactly how far
startup got. `run()` no longer swallows the traceback of a fatal startup
error: it now prints the full traceback (file + line number) via
`traceback.print_exc()` in addition to the structured log line, and always
exits with a non-zero status so Railway reports the deploy as failed instead
of leaving a container that looks "Online" but never came up.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal as signal_module
import sys
import traceback

import httpx
from redis.asyncio import Redis
from sqlalchemy import text

from whale_alpha.bot import create_bot
from whale_alpha.config import get_env
from whale_alpha.db.session import create_engine, create_session_factory
from whale_alpha.engines.discovery import start_discovery_loop
from whale_alpha.engines.price_alerts import start_price_alert_loop
from whale_alpha.engines.reconciliation import reconcile_pending_trades
from whale_alpha.engines.scheduler import start_scheduler
from whale_alpha.integrations.helius_webhook import start_webhook_server
from whale_alpha.integrations.solana_connection import create_connection
from whale_alpha.utils.logger import child_logger, configure_logging

log = child_logger("main")


async def main() -> None:
    # --- stage 1: environment -------------------------------------------
    env = get_env()
    configure_logging(env.LOG_LEVEL, env.NODE_ENV)
    log.info("Loading environment...")
    log.info("Environment loaded", node_env=env.NODE_ENV, log_level=env.LOG_LEVEL)

    # --- stage 2: PostgreSQL ----------------------------------------------
    log.info("Connecting PostgreSQL...")
    engine = create_engine(env)
    session_factory = create_session_factory(engine)
    # create_async_engine() is lazy — it does not actually open a connection.
    # Prove connectivity here with a real round-trip so a bad DATABASE_URL,
    # unreachable host, or auth failure surfaces immediately and loudly,
    # instead of silently deferring the first real error to whichever
    # handler happens to touch the DB first.
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    log.info("PostgreSQL connected")

    # --- stage 3: Redis -----------------------------------------------------
    log.info("Connecting Redis...")
    redis = Redis.from_url(env.REDIS_URL)
    await redis.ping()
    log.info("Redis connected")

    log.info("Database Ready.")

    # --- stage 4: startup reconciliation -------------------------------
    reconciliation_connection = create_connection(env)
    try:
        async with session_factory() as session:
            summary = await reconcile_pending_trades(session, reconciliation_connection)
        log.info("Startup reconciliation complete", **summary)
    finally:
        await reconciliation_connection.close()

    http_client = httpx.AsyncClient(timeout=30.0)

    log.info("Loading Solana Monitor...")
    solana_connection = create_connection(env)
    log.info("Solana Monitor ready")

    log.info("Loading Telegram Bot...")
    bot, dp = create_bot(env, redis, session_factory, http_client)
    log.info("Telegram Bot loaded")

    log.info("Loading Background Workers...")
    stop_scheduler = start_scheduler(env, session_factory, bot, http_client, solana_connection)
    stop_price_alerts = start_price_alert_loop(env, session_factory, bot, http_client)
    stop_discovery: object | None = None
    if env.DISCOVERY_ENABLED:
        stop_discovery = start_discovery_loop(env, session_factory, http_client, solana_connection)
    else:
        log.warning("Whale Wallet Discovery Engine disabled via DISCOVERY_ENABLED=false")
    log.info("Background Workers started")

    log.info("Loading Helius Webhook...")
    webhook_runner = await start_webhook_server(env, session_factory, http_client)
    log.info("Helius Webhook running")

    stop_event = asyncio.Event()

    def _handle_sigint() -> None:
        log.info("Shutting down...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_sigint)

    polling_task = asyncio.create_task(dp.start_polling(bot))
    log.info("Bot Started Successfully.", env=env.NODE_ENV)
    log.info("Webhook Server Running.", host=env.WEBHOOK_HOST, port=env.effective_webhook_port)
    log.info("Application Ready.")

    await stop_event.wait()

    polling_task.cancel()
    await stop_scheduler()
    await stop_price_alerts()
    if stop_discovery is not None:
        await stop_discovery()
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
        log.error("Fatal startup error", err=str(err), err_type=type(err).__name__)
        print("FATAL: Whale Alpha failed to start. Full traceback:", file=sys.stderr)
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        raise SystemExit(1) from err


if __name__ == "__main__":
    run()
