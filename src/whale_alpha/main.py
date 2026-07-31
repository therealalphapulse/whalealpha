"""Main entrypoint — port of src/index.ts.

--- NEW vs. the TS original (porting requirement #3) ---
Before the bot starts polling Telegram or the scheduler starts evaluating
signals, we run `reconcile_pending_trades` once against the database + a
Solana RPC connection, so any trade left mid-flight by a prior process
(crash, redeploy, OOM-kill) is resolved before normal operation resumes. This
did not exist in the TS version.
"""

from __future__ import annotations

import asyncio
import signal as signal_module

import httpx
from redis.asyncio import Redis

from whale_alpha.bot import create_bot
from whale_alpha.config import get_env
from whale_alpha.db.session import create_engine, create_session_factory
from whale_alpha.engines.reconciliation import reconcile_pending_trades
from whale_alpha.engines.scheduler import start_scheduler
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

    solana_connection = create_connection(env)
    try:
        async with session_factory() as session:
            summary = await reconcile_pending_trades(session, solana_connection)
        log.info("Startup reconciliation complete", **summary)
    finally:
        await solana_connection.close()

    http_client = httpx.AsyncClient(timeout=30.0)

    bot, dp = create_bot(env, redis, session_factory)

    stop_scheduler = start_scheduler(env, session_factory)

    stop_event = asyncio.Event()

    def _handle_sigint() -> None:
        log.info("Shutting down...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_sigint)
        except NotImplementedError:
            # add_signal_handler isn't available on some platforms (e.g. Windows);
            # Railway's runtime is Linux, so this is a defensive fallback only.
            pass

    polling_task = asyncio.create_task(dp.start_polling(bot))
    log.info("Whale Alpha bot started", env=env.NODE_ENV)

    await stop_event.wait()

    polling_task.cancel()
    await stop_scheduler()
    await http_client.aclose()
    await redis.aclose()
    await engine.dispose()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass


def run() -> None:
    try:
        asyncio.run(main())
    except Exception as err:  # noqa: BLE001 — mirrors the TS catch-all in main().catch(...)
        log.error("Fatal startup error", err=str(err))
        raise SystemExit(1) from err


if __name__ == "__main__":
    run()
