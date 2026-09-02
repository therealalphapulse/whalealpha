"""Whale Alpha production entrypoint â intelligence-only token screener."""
from __future__ import annotations

import asyncio
import contextlib
import signal as signal_module
import sys
import traceback

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text

from whale_alpha.bot import create_bot
from whale_alpha.config import get_env
from whale_alpha.db.session import create_engine, create_session_factory
from whale_alpha.integrations.solana_connection import create_connection
from whale_alpha.engines.screener import start_screener_loop
from whale_alpha.engines.price_alerts import start_price_alert_loop
from whale_alpha.utils.logger import child_logger, configure_logging

log = child_logger("main")


async def main() -> None:
    env = get_env()
    configure_logging(env.LOG_LEVEL, env.NODE_ENV)
    log.info("Whale Alpha token hunter starting", mode="INTELLIGENCE_ONLY", trading_enabled=env.ENABLE_LEGACY_TRADING)
    if env.ENABLE_LEGACY_TRADING:
        raise RuntimeError("ENABLE_LEGACY_TRADING must remain false in Whale Alpha token-screener production mode")

    engine = create_engine(env)
    session_factory = create_session_factory(engine)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    log.info("PostgreSQL connected")

    redis = Redis.from_url(env.REDIS_URL)
    redis_healthy = True
    try:
        await redis.ping()
        # PING can succeed while MISCONF rejects every write, so probe a real write.
        await redis.set("__whale_alpha_startup_probe__", "1", ex=30)
        log.info("Redis connected")
    except RedisError as err:
        redis_healthy = False
        log.error("Redis unavailable; continuing with in-memory bot FSM", err=str(err))

    http_client = httpx.AsyncClient(timeout=20.0)
    bot, dp = create_bot(env, redis, session_factory, http_client, use_redis_storage=redis_healthy)
    stop_hunter = None
    stop_price_alerts = start_price_alert_loop(env, session_factory, bot, http_client)
    solana_connection = None
    if env.TOKEN_HUNTER_ENABLED:
        solana_connection = create_connection(env)
        stop_hunter = start_screener_loop(env, session_factory, bot, http_client, solana_connection)
        log.info("DexScreener Token Screener started")
    else:
        log.warning("Token Screener disabled via TOKEN_HUNTER_ENABLED=false")

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("Shutdown requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_signal)

    polling_task = asyncio.create_task(dp.start_polling(bot))
    log.info("Telegram polling started")
    log.info("Application Ready â intelligence only; DexScreener screener active; no trading workers are started")
    await stop_event.wait()

    polling_task.cancel()
    if stop_hunter is not None:
        await stop_hunter()
    await stop_price_alerts()
    await http_client.aclose()
    if solana_connection is not None:
        await solana_connection.close()
    await redis.aclose()
    await engine.dispose()
    with contextlib.suppress(asyncio.CancelledError):
        await polling_task


def run() -> None:
    try:
        asyncio.run(main())
    except Exception as err:  # noqa: BLE001
        log.error("Fatal startup error", err=str(err), err_type=type(err).__name__)
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1) from err


if __name__ == "__main__":
    run()
