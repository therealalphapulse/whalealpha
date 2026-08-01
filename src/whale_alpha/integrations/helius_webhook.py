"""Inbound whale-buy ingestion — closes the "no code path at all" gap for
whale wallet tracking. `engines/monitor.ingest_wallet_buy_event` existed as a
ready-to-call function with nothing calling it; this module is the caller.

ASSUMPTION (flagged, same as the repo's other integration seams): this
targets Helius's "Enhanced" webhook payload shape, since Helius is the most
common managed indexer for this use case and the original TODOs explicitly
name it ("Helius webhooks / a geyser plugin / your own indexer"). If you use
a different indexer (Triton, your own geyser plugin, QuickNode streams,
etc.), swap `_extract_buy_events` for a parser matching that provider's
payload — everything downstream of it (`ingest_wallet_buy_event`) is
provider-agnostic.

Helius enhanced webhooks POST a JSON array of parsed transactions. Each
transaction we care about looks roughly like:

    {
      "type": "SWAP",
      "signature": "...",
      "timestamp": 1712345678,
      "tokenTransfers": [
        {"fromUserAccount": "...", "toUserAccount": "<wallet>",
         "mint": "<token_mint>", "tokenAmount": 1234.5}
      ],
      "events": {"swap": {"tokenOutputs": [...], "nativeInput": {...}}}
    }

We treat a transaction as a "buy" by a tracked wallet when a tokenTransfer's
`toUserAccount` matches a tracked wallet's address and the mint isn't SOL —
i.e. the wallet's token balance for that mint increased. USD sizing comes
from the price feed (Helius doesn't include USD value directly), so an event
is dropped (logged, not ingested) if the price feed can't resolve the mint —
better to miss one data point than record a fabricated dollar amount.

Verify this against a real Helius payload from your dashboard before
depending on it in production; enhanced-webhook shapes have changed before.
"""

from __future__ import annotations

from typing import Any

import httpx
from aiohttp import web
from sqlalchemy.ext.asyncio import async_sessionmaker

from whale_alpha.config import Env
from whale_alpha.engines.monitor import ingest_wallet_buy_event
from whale_alpha.integrations import price_feed
from whale_alpha.integrations.price_feed import SOL_MINT
from whale_alpha.utils.logger import child_logger

log = child_logger("heliusWebhook")

routes = web.RouteTableDef()


def _extract_buy_events(transaction: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns a list of {wallet_address, token_mint, amount_tokens, tx_signature}
    for every apparent "wallet received a non-SOL token" transfer in this tx.
    """
    signature = transaction.get("signature")
    if not signature:
        return []

    out: list[dict[str, Any]] = []
    for transfer in transaction.get("tokenTransfers") or []:
        to_account = transfer.get("toUserAccount")
        mint = transfer.get("mint")
        amount = transfer.get("tokenAmount")
        if not to_account or not mint or mint == SOL_MINT or amount is None:
            continue
        try:
            amount_tokens = float(amount)
        except (TypeError, ValueError):
            continue
        if amount_tokens <= 0:
            continue
        out.append(
            {
                "wallet_address": to_account,
                "token_mint": mint,
                "amount_tokens": amount_tokens,
                "tx_signature": signature,
            }
        )
    return out


def create_webhook_app(
    env: Env, session_factory: async_sessionmaker, http_client: httpx.AsyncClient
) -> web.Application:
    app = web.Application()

    async def handle_webhook(request: web.Request) -> web.Response:
        if env.HELIUS_WEBHOOK_SECRET:
            auth_header = request.headers.get("Authorization", "")
            if auth_header != env.HELIUS_WEBHOOK_SECRET:
                log.warning("Rejected webhook request with invalid/missing auth header")
                raise web.HTTPUnauthorized(text="invalid auth header")

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — any parse failure is just "bad request"
            raise web.HTTPBadRequest(text="invalid JSON body") from None

        transactions = payload if isinstance(payload, list) else [payload]

        ingested = 0
        skipped_no_price = 0
        for tx in transactions:
            if not isinstance(tx, dict):
                continue
            for candidate in _extract_buy_events(tx):
                price = await price_feed.get_price_usd(http_client, env, candidate["token_mint"])
                if price is None:
                    skipped_no_price += 1
                    log.debug(
                        "Skipping whale event — no price available",
                        mint=candidate["token_mint"],
                        tx=candidate["tx_signature"],
                    )
                    continue

                amount_usd = candidate["amount_tokens"] * price
                async with session_factory() as session:
                    try:
                        await ingest_wallet_buy_event(
                            session,
                            wallet_address=candidate["wallet_address"],
                            token_mint=candidate["token_mint"],
                            amount_tokens=candidate["amount_tokens"],
                            amount_usd=amount_usd,
                            tx_signature=candidate["tx_signature"],
                        )
                        ingested += 1
                    except Exception as err:  # noqa: BLE001 — one bad event shouldn't 500 the whole batch
                        # Most commonly a duplicate tx_signature (unique
                        # constraint) if Helius retries a delivery — safe to
                        # ignore rather than fail the webhook.
                        log.debug(
                            "Whale event ingestion skipped",
                            err=str(err),
                            tx=candidate["tx_signature"],
                        )

        log.info("Webhook processed", ingested=ingested, skipped_no_price=skipped_no_price)
        return web.json_response({"ingested": ingested, "skipped_no_price": skipped_no_price})

    app.router.add_post(env.WEBHOOK_PATH, handle_webhook)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_get("/healthz", health)

    return app


async def start_webhook_server(
    env: Env, session_factory: async_sessionmaker, http_client: httpx.AsyncClient
) -> web.AppRunner:
    """Starts the aiohttp webhook server as a background task. Returns the
    AppRunner so main.py can clean it up (`await runner.cleanup()`) on
    shutdown, mirroring how `start_scheduler` returns a `stop()` coroutine.
    """
    app = create_webhook_app(env, session_factory, http_client)
    runner = web.AppRunner(app)
    await runner.setup()
    port = env.effective_webhook_port
    site = web.TCPSite(runner, env.WEBHOOK_HOST, port)
    await site.start()
    log.info("Webhook server started", host=env.WEBHOOK_HOST, port=port, path=env.WEBHOOK_PATH)
    return runner
