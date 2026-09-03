"""
app_platform/gateway/webhook_entrypoint.py

Production, multi-replica entrypoint (Bible §3 Physical Deployment). Run
with:
    python -m app_platform.gateway.webhook_entrypoint

This is the concrete fix for the audit's core scalability finding
(§3/§11): Telegram long-polling (`getUpdates`) can only ever be consumed
by one process per bot token — running two polling processes against the
same token causes them to fight over updates, which is a hard Telegram
API constraint, not a design choice. That is the literal reason v3's
architecture has a scalability ceiling of exactly one instance.

Webhook mode inverts the delivery model: Telegram pushes updates via
HTTPS POST to a URL this process serves, so any number of stateless
replicas can sit behind a load balancer, each handling whichever updates
route to it — horizontal scaling becomes possible for the first time.

Requires:
  * WEBHOOK_URL — the public HTTPS URL Telegram should POST updates to
    (e.g. https://gateway.example.com/webhook)
  * WEBHOOK_SECRET — a random token used to validate incoming requests
    actually came from Telegram (X-Telegram-Bot-Api-Secret-Token header)
  * REDIS_URL — required in practice for this mode to be meaningfully
    multi-instance-safe, since FSM state must be shared (see
    app_platform/gateway/app.py); webhook mode will still run without it,
    falling back to in-memory FSM, but that defeats the purpose of running
    more than one replica.

This module has not been exercised against a live Telegram webhook
delivery in this environment (no network access, no public HTTPS
endpoint available here) — same NOT-YET-LIVE-VALIDATED caveat the audit
applied to other network-dependent modules it could not test directly.
It is written to aiogram 3.x's documented webhook integration
(`aiohttp.web` + `SimpleRequestHandler`), which is the library's standard,
supported pattern.
"""

from __future__ import annotations

import logging
import os

from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from infra.observability.logging_config import configure_logging
from infra.observability.metrics import configure_metrics
from infra.observability.error_tracking import configure_error_tracking
from infra.db.session import close_db
from app_platform.gateway.app import build_app, set_bot_commands

logger = logging.getLogger("AlphaPulse.Gateway.Webhook")

WEBHOOK_PATH = "/webhook"


async def on_startup(bot) -> None:
    webhook_url = os.getenv("WEBHOOK_URL")
    webhook_secret = os.getenv("WEBHOOK_SECRET")
    if not webhook_url or not webhook_secret:
        raise ValueError(
            "WEBHOOK_URL and WEBHOOK_SECRET are both required in webhook mode. "
            "Use app_platform.gateway.polling_entrypoint instead for local/dev."
        )

    await bot.set_webhook(
        url=f"{webhook_url.rstrip('/')}{WEBHOOK_PATH}",
        secret_token=webhook_secret,
        drop_pending_updates=True,
    )
    await set_bot_commands(bot)
    logger.info("Webhook registered at %s%s", webhook_url, WEBHOOK_PATH)


async def on_shutdown(bot) -> None:
    await bot.delete_webhook()
    await close_db()


def create_web_app() -> web.Application:
    configure_logging()
    configure_error_tracking()
    configure_metrics(port=9090)
    bot, dp = build_app()

    app = web.Application()
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=os.getenv("WEBHOOK_SECRET"),
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Lightweight liveness endpoint for the load balancer / orchestrator
    # (Bible §8: v3 had no health-check surface at all).
    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_get("/health", health)

    return app


if __name__ == "__main__":
    web.run_app(create_web_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
