"""
workers/combined_worker.py

Free-tier Railway variant (Bible §11 topology, adapted for a 2-service
plan limit). The v4 design calls for THREE separate processes: the
Bot Gateway (Telegram-facing, latency-sensitive) and two isolated worker
pools — Signal/Trading and Intelligence (see workers/signal_trading_worker.py
and workers/intelligence_worker.py for why they were split from the gateway
and from each other).

Railway's free tier only allows 2 services per project, so a 3-way split
isn't available. This file recombines the two worker pools back into a
single process — but critically, still keeps them OUT of the Gateway
process. The original v3 problem this whole split was fixing was the
Telegram polling loop sharing an event loop with ~10 background scanning/
trading loops, causing slow command responses. That contention goes away
as long as the Gateway is isolated; the two worker pools sharing a loop
with EACH OTHER was never the source of that problem, so recombining
them here is a safe compromise for the 2-service constraint.

Run with:
    python -m workers.combined_worker

If you ever upgrade past the free tier, switch back to running
workers/signal_trading_worker.py and workers/intelligence_worker.py as
 two separate services instead of this combined file.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import sitecustomize  # noqa: F401,E402
from workers.holder_runtime_bootstrap import install as install_holder_runtime_adapters

# Install holder compatibility/indexed fallback adapters deterministically
# before any worker loop can request holder data. This does not alter scoring
# thresholds; it only makes holder-data provider selection independent of
# Python import-hook timing.
install_holder_runtime_adapters()

from sqlalchemy import text
from infra.db.session import async_session, close_db
import workers.signal_trading_worker as signal_trading_worker
from workers.intelligence_worker import main as run_intelligence_worker
import domain.signals.enhanced_alert_runtime as enhanced_alert_runtime
from domain.signals.enhanced_alert_runtime import (
    enhanced_pump_radar_loop,
    enhanced_signal_lifecycle_loop,
)
from domain.signals.reactivation_expanded import fetch_expanded_reactivation_candidates
from providers.rpc.helius_request_manager import helius_manager, PRIORITY_LOW
from providers.rpc.multi_rpc_manager import multi_rpc_manager

signal_trading_worker.pump_radar_loop = enhanced_pump_radar_loop
signal_trading_worker.signal_lifecycle_loop = enhanced_signal_lifecycle_loop
enhanced_alert_runtime.fetch_reactivation_candidates = fetch_expanded_reactivation_candidates

logger = logging.getLogger("AlphaPulse.Worker.Combined")


async def _reconcile_confirmed_alert_delivery() -> None:
    """Backfill the confirmed-delivery flag for the enhanced alert path.

    The enhanced PumpRadar runtime records successful Telegram message IDs in
    ``signal_tokens.message_ids_json`` but historically did not call
    ``mark_signal_alert_delivered()``. Real automation deliberately requires
    ``alert_delivered=True`` before buying, so those otherwise-valid alerts
    became invisible to the real-money automation engine.

    This is a narrow state-reconciliation safety net: only recent active
    signals with a non-empty successful-delivery message map are promoted.
    It does not alter signal selection, scoring, or trading filters.
    """
    while True:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(tzinfo=None)
            async with async_session() as session:
                result = await session.execute(
                    text("""
                        UPDATE signal_tokens
                        SET alert_delivered = TRUE,
                            alert_delivered_at = COALESCE(alert_delivered_at, NOW())
                        WHERE status = 'active'
                          AND alert_delivered = FALSE
                          AND signaled_at >= :cutoff
                          AND message_ids_json IS NOT NULL
                          AND message_ids_json <> '{}'
                          AND message_ids_json <> ''
                    """),
                    {"cutoff": cutoff},
                )
                promoted = int(result.rowcount or 0)
                await session.commit()

            if promoted:
                logger.info(
                    "[AlertDeliveryReconcile] promoted %d recently delivered signal(s) to alert_delivered=true",
                    promoted,
                )
        except Exception as exc:
            logger.warning(
                "[AlertDeliveryReconcile] reconciliation failed (non-fatal): %s",
                exc,
            )

        await asyncio.sleep(5)


async def _run_usdc_holder_control_probe() -> None:
    """Read-only mature-token control probe; never feeds the signal pipeline."""
    mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGkZwyTDt1v"
    filters = [
        {"dataSize": 165},
        {"memcmp": {"offset": 0, "bytes": mint}},
    ]
    logger.info("[HolderControl] Starting read-only USDC holder control probe")

    v2_payload = {
        "jsonrpc": "2.0",
        "id": "holder-control-v2",
        "method": "getProgramAccountsV2",
        "params": [
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            {"encoding": "jsonParsed", "filters": filters, "limit": 100},
        ],
    }
    legacy_payload = {
        "jsonrpc": "2.0",
        "id": "holder-control-legacy",
        "method": "getProgramAccounts",
        "params": [
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            {"encoding": "jsonParsed", "filters": filters},
        ],
    }

    try:
        v2 = await helius_manager.request_json(
            "POST",
            "solana-json-rpc:getProgramAccountsV2",
            json_body=v2_payload,
            priority=PRIORITY_LOW,
            timeout=20,
            context=f"holder_accounts_v2:control:{mint}",
            retry_on_empty_result=False,
        )
        v2_result = v2.get("result") if isinstance(v2, dict) else None
        if isinstance(v2_result, dict):
            v2_accounts = v2_result.get("accounts")
            v2_count = len(v2_accounts) if isinstance(v2_accounts, list) else -1
            logger.info(
                "[HolderControl] USDC Helius V2: http_success=true "
                f"result_type=dict accounts_count={v2_count} "
                f"pagination_key_present={bool(v2_result.get('paginationKey'))}"
            )
        elif isinstance(v2_result, list):
            logger.info(
                "[HolderControl] USDC Helius V2: http_success=true "
                f"result_type=list accounts_count={len(v2_result)}"
            )
        else:
            logger.warning(
                "[HolderControl] USDC Helius V2: no usable result "
                f"result_type={type(v2_result).__name__}"
            )
    except Exception as exc:
        logger.warning("[HolderControl] USDC Helius V2 probe failed safely: %s", type(exc).__name__)

    try:
        legacy = await multi_rpc_manager.request_json(
            "POST",
            "solana-json-rpc:getProgramAccounts",
            json_body=legacy_payload,
            priority=PRIORITY_LOW,
            timeout=20,
            context=f"holder_control_legacy:{mint}",
            retry_on_empty_result=True,
        )
        legacy_result = legacy.get("result") if isinstance(legacy, dict) else None
        legacy_count = len(legacy_result) if isinstance(legacy_result, list) else -1
        logger.info(
            "[HolderControl] USDC legacy MultiRPC: "
            f"result_type={type(legacy_result).__name__} accounts_count={legacy_count}"
        )
    except Exception as exc:
        logger.warning("[HolderControl] USDC legacy MultiRPC probe failed safely: %s", type(exc).__name__)


async def main() -> None:
    logger.info(
        "Starting AlphaPulse v4 combined worker process (free-tier mode). "
        "Runs the Signal/Trading and Intelligence worker pools together in "
        "one process, separate from the Bot Gateway. Signal filters are "
        "unchanged; enhanced downstream signal/quote delivery is enabled."
    )

    try:
        await _run_usdc_holder_control_probe()
    except Exception as exc:
        logger.warning("[HolderControl] Probe failed safely: %s", type(exc).__name__)

    try:
        await asyncio.gather(
            signal_trading_worker.main(),
            run_intelligence_worker(),
            _reconcile_confirmed_alert_delivery(),
        )
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
