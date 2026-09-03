"""
workers/intelligence_worker.py

NEW in v4 (Bible §11). Run with:
    python -m workers.intelligence_worker

Replaces the other half of v3's `main.py` loop soup: the Premium
Intelligence Engine (Smart Wallet Discovery, Wallet Intelligence Scoring,
Wallet Maintenance, Premium Wallet Monitor) and KOL wallet sync.

v3 already isolated the Premium engine behind a feature flag
(`PREMIUM_BACKGROUND_SCHEDULERS_ENABLED`, default false) but ran it in
the same process and event loop as everything else when enabled (audit
§1/§7). v4 keeps the flag (nothing about when Premium features are
considered ready to enable changes here) but makes the isolation real: a
slow wallet-discovery scan in this worker can no longer contend with the
Signal/Trading worker's latency-sensitive loops, because they are
different processes.

Note: v3's `main.py` actually started `kol_provider_sync_loop`
unconditionally, not gated behind the Premium flag — it's grouped here
with the Premium engine instead because the Bible's worker-pool table
(§11) explicitly places KOL sync in the Intelligence pool (thematically
an intelligence-layer job, not a latency-sensitive user-facing one). KOL
sync's own behavior is unaffected by PREMIUM_BACKGROUND_SCHEDULERS_ENABLED
and still runs regardless of that flag, exactly as in v3 — only its
process placement changed.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config.settings import (
    BOT_TOKEN,
    KOL_SYNC_INTERVAL_SECONDS,
    PREMIUM_BACKGROUND_SCHEDULERS_ENABLED,
)
from infra.observability.logging_config import configure_logging
from infra.observability.metrics import configure_metrics
from infra.observability.error_tracking import configure_error_tracking
from infra.locks import run_as_leader

from domain.intelligence.kol_tracker import kol_provider_sync_loop
from domain.payments.premium_service import (
    premium_expiry_sweep_loop,
    start_premium_intelligence_engine,
)

logger = logging.getLogger("AlphaPulse.Worker.Intelligence")


async def main() -> None:
    configure_logging()
    configure_error_tracking()
    configure_metrics(port=9092)

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    logger.info("Intelligence worker starting...")

    jobs = [
        run_as_leader(
            "loop:kol_provider_sync",
            lambda: kol_provider_sync_loop(bot=bot, interval_seconds=KOL_SYNC_INTERVAL_SECONDS),
            lease_seconds=max(90, KOL_SYNC_INTERVAL_SECONDS // 2),
            renew_interval_seconds=30,
        ),
    ]

    if PREMIUM_BACKGROUND_SCHEDULERS_ENABLED:
        jobs.append(
            run_as_leader(
                "loop:premium_expiry_sweep",
                lambda: premium_expiry_sweep_loop(bot=bot, interval_seconds=3600),
                lease_seconds=90, renew_interval_seconds=30,
            )
        )
        jobs.append(
            run_as_leader(
                "loop:premium_intelligence_engine",
                lambda: start_premium_intelligence_engine(bot=bot),
                lease_seconds=90, renew_interval_seconds=30,
            )
        )
    else:
        logger.info(
            "PREMIUM_BACKGROUND_SCHEDULERS_ENABLED=false — Premium "
            "Intelligence Engine and Premium expiry sweep are idle in this "
            "worker. KOL sync is unaffected and still runs. Set "
            "PREMIUM_BACKGROUND_SCHEDULERS_ENABLED=true to enable Premium "
            "features (same flag and behavior as v3)."
        )

    await asyncio.gather(*jobs)


if __name__ == "__main__":
    asyncio.run(main())
