"""Professional trading policy for Whale Alpha."""
from __future__ import annotations

from dataclasses import dataclass

from whale_alpha.engines.risk import AutoTradingRules


@dataclass(frozen=True)
class FixedAutoPolicy:
    """Immutable production policy for signal-driven auto-buy."""
    amount_usd: float = 5.0
    max_slippage_bps: int = 150
    min_liquidity_usd: float = 10_000.0
    max_open_positions: int = 5
    max_daily_trades: int = 5
    max_daily_exposure_usd: float = 25.0
    cooldown_minutes: float = 15.0
    max_market_cap_usd: float | None = 5_000_000.0


FIXED_AUTO_POLICY = FixedAutoPolicy()
MANUAL_MIN_BUY_USD = 1.0
MANUAL_MAX_BUY_USD = 500.0
MANUAL_MIN_SLIPPAGE_BPS = 50
MANUAL_MAX_SLIPPAGE_BPS = 500


def fixed_auto_rules(enabled: bool) -> AutoTradingRules:
    p = FIXED_AUTO_POLICY
    return AutoTradingRules(
        enabled=enabled,
        max_slippage_bps=p.max_slippage_bps,
        min_liquidity_usd=p.min_liquidity_usd,
        max_open_positions=p.max_open_positions,
        max_daily_trades=p.max_daily_trades,
        max_daily_exposure_usd=p.max_daily_exposure_usd,
        cooldown_minutes=p.cooldown_minutes,
        fixed_trade_amount_usd=p.amount_usd,
        percent_allocation=None,
        max_market_cap_usd=p.max_market_cap_usd,
        token_blacklist=[],
    )


def validate_manual_buy(usd_amount: float, slippage_bps: int) -> str | None:
    if not MANUAL_MIN_BUY_USD <= usd_amount <= MANUAL_MAX_BUY_USD:
        return f"Manual buy amount must be between ${MANUAL_MIN_BUY_USD:g} and ${MANUAL_MAX_BUY_USD:g}."
    if not MANUAL_MIN_SLIPPAGE_BPS <= slippage_bps <= MANUAL_MAX_SLIPPAGE_BPS:
        return "Manual slippage must be between 0.5% and 5%."
    return None
