"""
app_platform/gateway/polling_entrypoint.py

Dev/single-instance entrypoint. Run with:
    python -m app_platform.gateway.polling_entrypoint

This is what `main.py` (kept at the repo root as a thin wrapper for
Railway/Procfile backward compatibility) actually calls. It reproduces
v3's `dp.start_polling(bot)` behavior exactly — long-polling, single
process — which remains a perfectly valid way to run AlphaPulse below
roughly the 1,000-user tier (Bible §9). It does NOT run the background
loops (alerts, signal scanning, trading engines) — those are started by
`workers/signal_trading_worker.py` and `workers/intelligence_worker.py`
as separate processes now (Bible §11), so this entrypoint's only job is
serving Telegram traffic.

For a truly single-process, single-command local/dev setup (the closest
equivalent to v3's `python main.py`), see `scripts/run_dev_all_in_one.py`,
which runs the gateway and both workers as asyncio tasks in one process —
useful for local development and this sandbox, but not how v4 is deployed
in production (Bible §3).
"""

from __future__ import annotations

import asyncio
import logging

from infra.observability.logging_config import configure_logging
from infra.observability.metrics import configure_metrics
from infra.observability.error_tracking import configure_error_tracking
from infra.db.session import close_db
from app_platform.gateway.app import build_app, set_bot_commands

logger = logging.getLogger("AlphaPulse.Gateway.Polling")


async def main() -> None:
    configure_logging()
    configure_error_tracking()
    configure_metrics(port=9090)
    bot, dp = build_app()

    await bot.delete_webhook(drop_pending_updates=True)
    await set_bot_commands(bot)

    logger.info("AlphaPulse v4 Bot Gateway starting (polling mode, single instance)...")

    try:
        await dp.start_polling(bot)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
