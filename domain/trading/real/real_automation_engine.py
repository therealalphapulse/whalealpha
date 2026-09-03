"""Real Trade Automation — signal-driven unattended Real Wallet buys."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from infra.db.session import async_session
from models.real_wallet import RealWallet
from models.real_autobuy_filter import RealAutoBuyFilter
from models.real_trade import RealTrade
from models.signal_token import SignalToken
from domain.trading.real.solana_wallet import (
    get_wallet_settings,
    register_auto_spend,
    release_auto_spend,
    register_auto_buy,
    release_auto_buy,
)
from domain.trading.real import real_trade_engine, real_exit_engine
from domain.trading.real.jupiter_swap import WRAPPED_SOL_MINT
from providers.marketdata.dexscreener import get_token_card_info

logger = logging.getLogger("AlphaPulse.RealAutomationEngine")

SIGNAL_LOOKBACK_MINUTES = 20
DEFAULT_SOL_PER_TRADE = 0.1
DEFAULT_AUTO_BUY_AMOUNT_USDT = 10.0
DEFAULT_DAILY_AUTO_BUY_LIMIT = 5
_FAILURE_COOLDOWN_STEPS_SECONDS = [120, 600, 1800]
_recent_failures: dict[tuple[int, str], tuple[float, int]] = {}

# auto_buy_signal_source component model -- extends the historical flat
# "new" / "redelivered" / "both" values with a "first_milestone" source
# (item: First Milestone as an Auto-Buy source), while keeping every
# existing value and its behavior unchanged. "both" is kept as a legacy
# alias for "new_redelivered" so existing rows (default="both") keep
# matching exactly the New+Redelivered signals they always matched --
# never First Milestone, which no pre-existing row can have opted into.
_SIGNAL_SOURCE_COMPONENT_SETS: dict[str, frozenset[str]] = {
    "new": frozenset({"new"}),
    "redelivered": frozenset({"redelivered"}),
    "first_milestone": frozenset({"first_milestone"}),
    "new_redelivered": frozenset({"new", "redelivered"}),
    "new_first_milestone": frozenset({"new", "first_milestone"}),
    "redelivered_first_milestone": frozenset({"redelivered", "first_milestone"}),
    "new_redelivered_first_milestone": frozenset({"new", "redelivered", "first_milestone"}),
}
_SIGNAL_SOURCE_LEGACY_ALIASES: dict[str, frozenset[str]] = {
    "both": _SIGNAL_SOURCE_COMPONENT_SETS["new_redelivered"],
}
ALL_SIGNAL_SOURCE_VALUES = set(_SIGNAL_SOURCE_COMPONENT_SETS) | set(_SIGNAL_SOURCE_LEGACY_ALIASES)


def signal_source_components(filt: "RealAutoBuyFilter") -> frozenset[str]:
    """Resolves RealAutoBuyFilter.auto_buy_signal_source (a flat string) to
    the set of sources it covers. Unknown/empty values fall back to the
    "both" (New + Redelivered) default, matching pre-existing behavior."""
    value = getattr(filt, "auto_buy_signal_source", None) or "both"
    if value in _SIGNAL_SOURCE_LEGACY_ALIASES:
        return _SIGNAL_SOURCE_LEGACY_ALIASES[value]
    return _SIGNAL_SOURCE_COMPONENT_SETS.get(value, _SIGNAL_SOURCE_COMPONENT_SETS["new_redelivered"])


def _cooldown_active(user_id: int, contract: str) -> bool:
    entry = _recent_failures.get((user_id, contract))
    if not entry:
        return False
    last_failed_at, streak = entry
    step = min(streak - 1, len(_FAILURE_COOLDOWN_STEPS_SECONDS) - 1)
    return (asyncio.get_event_loop().time() - last_failed_at) < _FAILURE_COOLDOWN_STEPS_SECONDS[step]


def _record_failure(user_id: int, contract: str) -> None:
    _, streak = _recent_failures.get((user_id, contract), (0.0, 0))
    _recent_failures[(user_id, contract)] = (asyncio.get_event_loop().time(), streak + 1)


def _clear_failure(user_id: int, contract: str) -> None:
    _recent_failures.pop((user_id, contract), None)


async def get_or_create_filter(user_id: int) -> RealAutoBuyFilter:
    async with async_session() as session:
        result = await session.execute(select(RealAutoBuyFilter).where(RealAutoBuyFilter.user_id == user_id))
        filt = result.scalar_one_or_none()
        if filt:
            return filt
        filt = RealAutoBuyFilter(
            user_id=user_id,
            auto_buy_amount_usdt=DEFAULT_AUTO_BUY_AMOUNT_USDT,
            daily_auto_buy_limit=DEFAULT_DAILY_AUTO_BUY_LIMIT,
            sol_per_trade=DEFAULT_SOL_PER_TRADE,
        )
        session.add(filt)
        await session.commit()
        await session.refresh(filt)
        return filt


async def update_filter(user_id: int, field: str, value) -> bool:
    allowed_fields = {
        "min_score", "min_market_cap", "max_market_cap", "min_liquidity_usd",
        "max_bundle_pct", "max_dev_holding_pct", "sol_per_trade",
        "auto_buy_amount_usdt", "take_profit_pct", "stop_loss_pct",
        "daily_auto_buy_limit", "allow_multiple_positions_same_token",
        "auto_buy_signal_source",
    }
    if field not in allowed_fields:
        return False
    if field == "daily_auto_buy_limit":
        value = int(value)
        if not 1 <= value <= 20:
            return False
    if field == "auto_buy_signal_source" and value not in ALL_SIGNAL_SOURCE_VALUES:
        return False
    if field in {"auto_buy_amount_usdt", "take_profit_pct", "stop_loss_pct"} and value is not None and float(value) <= 0:
        return False
    await get_or_create_filter(user_id)
    async with async_session() as session:
        result = await session.execute(select(RealAutoBuyFilter).where(RealAutoBuyFilter.user_id == user_id))
        filt = result.scalar_one_or_none()
        if not filt:
            return False
        setattr(filt, field, value)
        await session.commit()
        return True


def candidate_matches_filter(signal: SignalToken, filt: RealAutoBuyFilter) -> tuple[bool, str | None]:
    if not filt.has_active_filters():
        return True, None
    score = signal.entry_score
    if filt.min_score is not None and (score is None or score < filt.min_score):
        return False, f"score {score} below min {filt.min_score}"
    mcap = signal.current_market_cap or signal.entry_market_cap
    if filt.min_market_cap is not None and (mcap is None or mcap < filt.min_market_cap):
        return False, "market cap below minimum"
    if filt.max_market_cap is not None and (mcap is None or mcap > filt.max_market_cap):
        return False, "market cap above maximum"
    liquidity = signal.current_liquidity or signal.entry_liquidity
    if filt.min_liquidity_usd is not None and (liquidity is None or liquidity < filt.min_liquidity_usd):
        return False, "liquidity below minimum"
    if filt.max_bundle_pct is not None and (signal.bundle_pct or 0) > filt.max_bundle_pct:
        return False, "bundle % above maximum"
    if filt.max_dev_holding_pct is not None and (signal.dev_holding_pct or 0) > filt.max_dev_holding_pct:
        return False, "dev holding % above maximum"
    return True, None


def _signal_source_matches(signal: SignalToken, filt: RealAutoBuyFilter) -> bool:
    """Gate the existing New/Redelivered signal-scan auto-buy path
    (scan_and_execute / _try_auto_buy below) by which kind of Signal Alert
    this token got.

    "new"        -- only signals delivered on the very first attempt.
    "redelivered" -- only signals whose alert had to be retried (see
                     SignalToken.was_redelivered /
                     pump_radar.redeliver_undelivered_signal_alerts()).
    Both selected (including the "both" legacy alias) -- no constraint,
    matching pre-existing behavior exactly.

    If the filter's auto_buy_signal_source selects ONLY "first_milestone"
    (no "new"/"redelivered" component), this scan path never matches --
    First Milestone has its own dedicated trigger, see
    run_first_milestone_auto_buy() below, and must never also fire off a
    token's initial New/Redelivered signal.
    """
    components = signal_source_components(filt)
    new_or_redelivered = components & {"new", "redelivered"}
    if not new_or_redelivered:
        return False
    if signal.was_redelivered:
        return "redelivered" in new_or_redelivered
    return "new" in new_or_redelivered


async def _already_auto_bought(user_id: int, contract: str) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(RealTrade.id).where(
                RealTrade.user_id == user_id,
                RealTrade.contract == contract,
                RealTrade.source == "automation",
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def _has_open_position(user_id: int, contract: str) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(RealTrade.id).where(
                RealTrade.user_id == user_id,
                RealTrade.contract == contract,
                RealTrade.status == "open",
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def _get_recent_active_signals(limit: int = 50) -> list[SignalToken]:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=SIGNAL_LOOKBACK_MINUTES)).replace(tzinfo=None)
    async with async_session() as session:
        result = await session.execute(
            select(SignalToken)
            .where(
                SignalToken.status == "active",
                SignalToken.signaled_at >= cutoff,
                SignalToken.alert_delivered == True,  # noqa: E712
            )
            .order_by(SignalToken.signaled_at.desc())
            .limit(limit)
        )
        return result.scalars().all()


async def _get_automation_enabled_wallets() -> list[RealWallet]:
    async with async_session() as session:
        result = await session.execute(
            select(RealWallet).where(
                RealWallet.is_active == True,  # noqa: E712
                RealWallet.auto_trading_enabled == True,  # noqa: E712
                RealWallet.auto_kill_switch == False,  # noqa: E712
            )
        )
        return result.scalars().all()


async def _notify(bot, user_id: int, text: str) -> None:
    if bot is None:
        return
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Could not notify user {user_id}: {e}")


async def _get_sol_usd_price() -> float | None:
    try:
        info = await get_token_card_info(WRAPPED_SOL_MINT)
        if info and info.get("price") not in (None, "N/A"):
            price = float(info["price"])
            return price if price > 0 else None
    except Exception as e:
        logger.warning("Unable to resolve SOL/USD price for USDT auto-buy: %s", e)
    return None


async def _resolve_auto_buy_sol_amount(filt: RealAutoBuyFilter) -> float | None:
    """Convert the user's canonical USDT amount to SOL at execution time."""
    amount_usdt = filt.auto_buy_amount_usdt
    if amount_usdt is not None:
        if amount_usdt <= 0:
            return None
        sol_price = await _get_sol_usd_price()
        if sol_price is None:
            return None
        return amount_usdt / sol_price
    # Legacy rows created before the USDT setting existed continue safely
    # using their stored SOL amount until the user chooses a custom amount.
    return filt.sol_per_trade if filt.sol_per_trade and filt.sol_per_trade > 0 else DEFAULT_SOL_PER_TRADE


async def _try_auto_buy(bot, wallet: RealWallet, signal: SignalToken, filt: RealAutoBuyFilter) -> None:
    if not filt.allow_multiple_positions_same_token and await _has_open_position(wallet.user_id, signal.contract):
        return
    if await _already_auto_bought(wallet.user_id, signal.contract):
        return
    if _cooldown_active(wallet.user_id, signal.contract):
        return
    if not _signal_source_matches(signal, filt):
        return

    await _execute_auto_buy(bot, wallet, signal, filt)


async def _try_first_milestone_auto_buy(bot, wallet: RealWallet, signal: SignalToken, filt: RealAutoBuyFilter) -> None:
    """First Milestone counterpart to _try_auto_buy above: same
    eligibility checks and the same shared execution pipeline
    (_execute_auto_buy), gated on "first_milestone" being one of this
    wallet's selected auto_buy_signal_source components instead of on
    _signal_source_matches (which only ever governs the New/Redelivered
    signal-scan path). Only ever called from run_first_milestone_auto_buy()
    below, itself only ever called for a signal's first milestone."""
    if "first_milestone" not in signal_source_components(filt):
        return
    if not filt.allow_multiple_positions_same_token and await _has_open_position(wallet.user_id, signal.contract):
        return
    if await _already_auto_bought(wallet.user_id, signal.contract):
        return
    if _cooldown_active(wallet.user_id, signal.contract):
        return

    await _execute_auto_buy(bot, wallet, signal, filt)


async def _execute_auto_buy(bot, wallet: RealWallet, signal: SignalToken, filt: RealAutoBuyFilter) -> None:
    """Shared eligibility-check + execution body for every auto-buy
    source (New/Redelivered signal scan and First Milestone alike). Every
    existing constraint, quality filter, and wallet/risk/cooldown/balance/
    daily-limit protection below is unchanged and applies identically
    regardless of which source triggered the call."""
    matches, _ = candidate_matches_filter(signal, filt)
    if not matches:
        return

    sol_amount = await _resolve_auto_buy_sol_amount(filt)
    price = signal.current_price or signal.entry_price
    if not sol_amount or sol_amount <= 0 or not price or price <= 0:
        return

    buy_slot = await register_auto_buy(wallet.user_id, int(filt.daily_auto_buy_limit or DEFAULT_DAILY_AUTO_BUY_LIMIT))
    if not buy_slot["ok"]:
        return

    spend_check = await register_auto_spend(wallet.user_id, sol_amount)
    if not spend_check["ok"]:
        await release_auto_buy(wallet.user_id)
        return

    settings = await get_wallet_settings(wallet.user_id)
    result = await real_trade_engine.execute_real_buy(
        user_id=wallet.user_id,
        contract=signal.contract,
        name=signal.name or "",
        symbol=signal.symbol or "???",
        current_price=price,
        sol_amount=sol_amount,
        slippage_bps=settings["slippage_bps"],
        priority_fee_tier=settings["priority_fee_tier"],
        source="automation",
    )

    if not result["ok"]:
        await release_auto_spend(wallet.user_id, sol_amount)
        await release_auto_buy(wallet.user_id)
        _record_failure(wallet.user_id, signal.contract)
        logger.warning("Automated buy failed for user %s on %s: %s", wallet.user_id, signal.contract, result["reason"])
        return

    _clear_failure(wallet.user_id, signal.contract)
    trade = result["trade"]

    # Custom TP/SL are attached only after the on-chain buy is successfully
    # recorded, so no exit rule can exist for a position that wasn't bought.
    if filt.take_profit_pct and filt.take_profit_pct > 0:
        try:
            await real_exit_engine.create_rule(wallet.user_id, trade.id, "tp", float(filt.take_profit_pct), 1.0)
        except Exception as e:
            logger.error("Failed to attach TP to automated trade %s: %s", trade.id, e)
    if filt.stop_loss_pct and filt.stop_loss_pct > 0:
        try:
            await real_exit_engine.create_rule(wallet.user_id, trade.id, "sl", float(filt.stop_loss_pct), 1.0)
        except Exception as e:
            logger.error("Failed to attach SL to automated trade %s: %s", trade.id, e)

    await _notify(
        bot, wallet.user_id,
        f"🤖 <b>Automation bought {trade.symbol or ''}</b>\n"
        f"Auto-buy amount: ${float(filt.auto_buy_amount_usdt or 0):.2f} USDT\n"
        f"Spent: {trade.sol_spent:.4f} SOL\n"
        f"Received: {trade.token_quantity:,.2f} {trade.symbol or ''}\n"
        f"TP: {float(filt.take_profit_pct):g}% | SL: {float(filt.stop_loss_pct):g}%\n"
        f"Tx: <code>{result['signature']}</code>\n\n"
        f"Manage this position from /realwallet.",
    )


async def scan_and_execute(bot=None) -> int:
    wallets = await _get_automation_enabled_wallets()
    if not wallets:
        return 0
    signals = await _get_recent_active_signals()
    if not signals:
        return 0
    attempts = 0
    for wallet in wallets:
        try:
            filt = await get_or_create_filter(wallet.user_id)
            for signal in signals:
                try:
                    await _try_auto_buy(bot, wallet, signal, filt)
                    attempts += 1
                except Exception as e:
                    logger.error("Automation attempt failed for user %s on %s: %s", wallet.user_id, signal.contract, e)
        except Exception as e:
            logger.error("Automation scan failed for user %s: %s", wallet.user_id, e)
    return attempts


async def real_automation_loop(bot=None, interval_seconds: int = 20):
    logger.info("🤖 Real Trade Automation loop starting...")
    while True:
        try:
            await scan_and_execute(bot=bot)
        except Exception as e:
            logger.error("Real automation loop error (non-fatal): %s", e)
        await asyncio.sleep(interval_seconds)


async def run_first_milestone_auto_buy(bot, signal: SignalToken) -> int:
    """First Milestone as an Auto-Buy source (item 2/3).

    Called exactly once from domain/signals/signal_tracker.py::
    send_milestone_alert, only for a signal's very first milestone alert
    -- the caller determines "first milestone" atomically with its own
    SignalEvent dedup insert, so this can never be invoked twice, and is
    never invoked for any subsequent milestone on that same signal. Fires
    the existing auto-buy execution pipeline (_execute_auto_buy, shared
    with the New/Redelivered scan path in scan_and_execute above) for
    every automation-enabled wallet whose auto_buy_signal_source includes
    "first_milestone", subject to every existing eligibility/execution
    constraint -- quality filters, wallet/risk/cooldown/balance/daily-limit
    protections -- unchanged. Wallets that haven't selected
    "first_milestone" are skipped, same as before this source existed.
    """
    wallets = await _get_automation_enabled_wallets()
    if not wallets:
        return 0
    attempts = 0
    for wallet in wallets:
        try:
            filt = await get_or_create_filter(wallet.user_id)
            await _try_first_milestone_auto_buy(bot, wallet, signal, filt)
            attempts += 1
        except Exception as e:
            logger.error("First Milestone automation attempt failed for user %s on %s: %s", wallet.user_id, signal.contract, e)
    return attempts