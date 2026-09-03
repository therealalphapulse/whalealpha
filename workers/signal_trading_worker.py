"""
workers/signal_trading_worker.py

NEW in v4 (Bible §11 — Background Job Architecture). Run with:
    python -m workers.signal_trading_worker

Replaces the half of v3's `main.py` loop soup that is user-facing and
latency-sensitive: signal scanning/alerting and every real-money
automation engine. This worker runs as its own deployable process,
entirely separate from `app_platform.gateway`.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config.settings import BOT_TOKEN
from infra.observability.logging_config import configure_logging
from infra.observability.metrics import configure_metrics
from infra.observability.error_tracking import configure_error_tracking
from infra.locks import run_as_leader
from domain.signals.keyboard_provider import set_keyboard_factory
from domain.intelligence.holder_state import install as install_holder_state_normalizer

from domain.signals.alert_engine import alert_loop
from domain.signals.pump_radar import pump_radar_loop
from domain.signals.signal_tracker import signal_lifecycle_loop, scheduled_broadcast_loop
from domain.trading.paper.paper_monitor import paper_monitor_loop
from domain.trading.real.solana_wallet import migrate_real_wallet_schema
from domain.trading.real.real_dca_engine import real_dca_scheduler_loop
from domain.trading.real.real_automation_engine import real_automation_loop
from domain.trading.real.real_exit_engine import real_exit_engine_loop
from domain.trading.real.real_limit_order_engine import real_limit_order_engine_loop
from domain.payments.premium_payments import payment_expiry_sweep_loop

logger = logging.getLogger("AlphaPulse.Worker.SignalTrading")

# Holder evidence is installed before any worker loop starts. These are
# explicit installs rather than relying on import-hook timing in
# sitecustomize: Helius V2 must normalize live response shapes, and the
# indexed Solana Tracker / Birdeye fallbacks must be available as the real
# production fallback when RPC returns no usable snapshot. Neither adapter
# changes signal thresholds or safety gates.
#
# Phase 3.2 fix: this used to call a module that doesn't exist in this
# repository (domain.intelligence._bitquery_holder_fallback), so the
# import inside the try/except always failed and was silently swallowed
# down to a warning log -- meaning this worker (the actual deployed
# production process, per Railway's configured start command) never
# installed ANY indexed holder fallback, Solana Tracker included, despite
# SOLANA_TRACKER_API_KEY being configured. workers/holder_runtime_bootstrap
# already implements and tests the correct, working chain (Helius V2 ->
# Solana Tracker -> Birdeye) and is used by workers/combined_worker.py;
# this now uses the same single source of truth instead of a second,
# broken, ad-hoc install sequence.
install_holder_state_normalizer()
try:
    from domain.intelligence._holder_v2_compat import install as install_holder_v2_compat
    install_holder_v2_compat()
except Exception as exc:
    logger.warning("[HolderDiag] Helius V2 compatibility adapter install failed: %s", exc)
try:
    from workers.holder_runtime_bootstrap import install as install_holder_runtime_adapters
    install_holder_runtime_adapters()
except Exception as exc:
    logger.warning("[HolderDiag] Indexed holder fallback (Solana Tracker/Birdeye) install failed: %s", exc)

# Discovery is also explicitly installed here so the PumpRadar feed does not
# depend on import-hook timing. It expands GeckoTerminal new/trending pages
# while leaving every downstream market, security, holder, scoring and risk
# gate unchanged.
try:
    from domain.signals._radar_discovery_adapter import install as install_radar_discovery
    install_radar_discovery()
except Exception as exc:
    logger.warning("[PumpRadar] Expanded discovery adapter install failed: %s", exc)


async def main() -> None:
    configure_logging()
    configure_error_tracking()
    configure_metrics(port=9091)

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    from app_platform.keyboards.token_actions import token_actions_keyboard
    set_keyboard_factory(token_actions_keyboard)

    try:
        await migrate_real_wallet_schema()
    except Exception as e:
        logger.error(f"RealWallet schema migration failed at worker startup: {e}")

    logger.info("Signal/Trading worker starting...")

    jobs = [
        run_as_leader("loop:alert_engine", lambda: alert_loop(bot),
                       lease_seconds=90, renew_interval_seconds=30),
        run_as_leader("loop:pump_radar", lambda: pump_radar_loop(bot, interval_seconds=45),
                       lease_seconds=90, renew_interval_seconds=30),
        run_as_leader("loop:signal_lifecycle", lambda: signal_lifecycle_loop(bot, interval_seconds=45),
                       lease_seconds=90, renew_interval_seconds=30),
        run_as_leader("loop:scheduled_broadcasts", lambda: scheduled_broadcast_loop(bot, interval_seconds=120),
                       lease_seconds=90, renew_interval_seconds=30),
        run_as_leader("loop:paper_monitor", lambda: paper_monitor_loop(bot, interval_seconds=30),
                       lease_seconds=90, renew_interval_seconds=30),
        run_as_leader("loop:real_dca", lambda: real_dca_scheduler_loop(bot, interval_seconds=30),
                       lease_seconds=90, renew_interval_seconds=30),
        run_as_leader("loop:real_automation", lambda: real_automation_loop(bot, interval_seconds=20),
                       lease_seconds=90, renew_interval_seconds=30),
        run_as_leader("loop:real_exit_engine", lambda: real_exit_engine_loop(bot, interval_seconds=20),
                       lease_seconds=90, renew_interval_seconds=30),
        run_as_leader("loop:real_limit_orders", lambda: real_limit_order_engine_loop(bot, interval_seconds=20),
                       lease_seconds=90, renew_interval_seconds=30),
        run_as_leader("loop:payment_expiry_sweep", lambda: payment_expiry_sweep_loop(interval_seconds=900),
                       lease_seconds=90, renew_interval_seconds=30),
    ]

    await asyncio.gather(*jobs)


if __name__ == "__main__":
    asyncio.run(main())
