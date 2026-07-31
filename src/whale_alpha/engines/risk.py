"""Risk engine: the gatekeeper for Auto Trading.

Exact port of src/engines/risk/riskEngine.ts. A qualified Signal is necessary
but not sufficient to trade — every configured user rule must also pass here,
server-side, so a compromised or buggy client can never bypass limits.

HARD REQUIREMENT: every check, order, and reason code below matches the TS
source exactly (see tests/unit/test_risk_engine.py, ported case-for-case from
tests/unit/riskEngine.test.ts).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from whale_alpha.engines.signal import SignalCandidate


@dataclass
class AutoTradingRules:
    enabled: bool
    max_slippage_bps: int
    min_liquidity_usd: float
    max_open_positions: int
    max_daily_trades: int
    max_daily_exposure_usd: float
    cooldown_minutes: float
    fixed_trade_amount_usd: float | None = None
    percent_allocation: float | None = None
    max_market_cap_usd: float | None = None
    token_blacklist: list[str] = field(default_factory=list)


@dataclass
class UserTradingState:
    open_positions: int
    trades_today: int
    exposure_usd_today: float
    last_trade_at: datetime | None
    portfolio_value_usd: float


@dataclass
class RiskCheckResult:
    approved: bool
    reasons: list[str]  # populated when NOT approved
    proposed_trade_usd: float


def evaluate_auto_trade(
    signal: SignalCandidate,
    market_cap_usd: float | None,
    liquidity_usd: float | None,
    rules: AutoTradingRules,
    state: UserTradingState,
) -> RiskCheckResult:
    reasons: list[str] = []

    if not rules.enabled:
        reasons.append("AUTO_TRADING_DISABLED")

    if signal.token_mint in rules.token_blacklist:
        reasons.append("TOKEN_BLACKLISTED")

    if (
        rules.max_market_cap_usd is not None
        and market_cap_usd is not None
        and market_cap_usd > rules.max_market_cap_usd
    ):
        reasons.append("MARKET_CAP_EXCEEDS_LIMIT")

    if liquidity_usd is not None and liquidity_usd < rules.min_liquidity_usd:
        reasons.append("LIQUIDITY_BELOW_MINIMUM")

    if state.open_positions >= rules.max_open_positions:
        reasons.append("MAX_OPEN_POSITIONS_REACHED")

    if state.trades_today >= rules.max_daily_trades:
        reasons.append("MAX_DAILY_TRADES_REACHED")

    if state.last_trade_at is not None:
        now_ms = time.time() * 1000
        minutes_since_last_trade = (now_ms - state.last_trade_at.timestamp() * 1000) / 60000
        if minutes_since_last_trade < rules.cooldown_minutes:
            reasons.append("COOLDOWN_ACTIVE")

    # Determine proposed trade size.
    proposed_trade_usd = 0.0
    if rules.fixed_trade_amount_usd:
        proposed_trade_usd = rules.fixed_trade_amount_usd
    elif rules.percent_allocation:
        proposed_trade_usd = state.portfolio_value_usd * (rules.percent_allocation / 100)
    else:
        reasons.append("NO_POSITION_SIZING_RULE_CONFIGURED")

    if state.exposure_usd_today + proposed_trade_usd > rules.max_daily_exposure_usd:
        reasons.append("MAX_DAILY_EXPOSURE_EXCEEDED")

    if proposed_trade_usd <= 0:
        reasons.append("INVALID_TRADE_SIZE")

    return RiskCheckResult(
        approved=len(reasons) == 0,
        reasons=reasons,
        proposed_trade_usd=proposed_trade_usd,
    )
