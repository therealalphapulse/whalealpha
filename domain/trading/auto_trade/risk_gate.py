"""domain/trading/auto_trade/risk_gate.py

§6 (User-Level Auto-Trade Decision) and §7 (Pre-Trade Execution Risk
Gate). Two distinct gates, kept as two functions:

  evaluate_user_policy() -- "should THIS user trade THIS signal at
      all", using only policy/DB state (cheap, no network calls).
  evaluate_execution_risk() -- "is it currently SAFE to execute this
      trade right now", using live wallet/market data (more
      expensive, only run once the cheap gate has already passed).

Every rejection returns an explicit RejectionReason (§6: "do not
silently drop trade opportunities").
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from models.auto_trade_policy import AutoTradePolicy
from domain.trading.real.robinhood_swap import get_native_balance, SwapError
from domain.trading.real.robinhood_wallet import get_real_wallet

from .constants import RejectionReason, BUY_NETWORK_RESERVE_WEI
from .signal_adapter import AutoTradeSignal
from . import policy_service
from . import position_manager

logger = logging.getLogger("AlphaPulse.AutoTrade.RiskGate")


class GateResult:
    def __init__(self, authorized: bool, reason: str | None = None, detail: str | None = None):
        self.authorized = authorized
        self.reason = reason
        self.detail = detail

    def __bool__(self):
        return self.authorized

    def __repr__(self):
        return f"<GateResult authorized={self.authorized} reason={self.reason}>"


async def evaluate_user_policy(user_id: int, policy: AutoTradePolicy, signal: AutoTradeSignal) -> GateResult:
    """§6 -- cheap, DB-only checks. Does not touch the network."""
    if not policy.auto_trade_enabled:
        return GateResult(False, RejectionReason.AUTO_TRADE_DISABLED)
    if policy.kill_switch:
        return GateResult(False, RejectionReason.KILL_SWITCH_ACTIVE)

    if not policy_service.signal_is_after_activation(signal.detected_at, policy):
        return GateResult(False, RejectionReason.SIGNAL_BEFORE_ACTIVATION)

    if policy.has_tier_filter() and signal.tier not in policy.allowed_tiers_set():
        return GateResult(False, RejectionReason.SIGNAL_TIER_NOT_ALLOWED, f"tier={signal.tier}")

    if policy.min_score is not None and (signal.score is None or signal.score < policy.min_score):
        return GateResult(False, RejectionReason.SIGNAL_TIER_NOT_ALLOWED, f"score {signal.score} below min {policy.min_score}")

    if policy.cooldown_seconds and policy.cooldown_seconds > 0:
        last_trade_at = await position_manager.get_last_trade_at(user_id)
        if last_trade_at is not None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            elapsed_seconds = (now - last_trade_at).total_seconds()
            if elapsed_seconds < policy.cooldown_seconds:
                remaining = policy.cooldown_seconds - elapsed_seconds
                return GateResult(False, RejectionReason.COOLDOWN_ACTIVE, f"{remaining:.0f}s remaining")

    if not policy.allow_multiple_positions_same_token and await position_manager.has_open_position_for_contract(user_id, signal.contract):
        return GateResult(False, RejectionReason.POSITION_ALREADY_OPEN)

    open_count = await position_manager.count_open_positions(user_id)
    if open_count >= int(policy.max_open_positions or 0):
        return GateResult(False, RejectionReason.MAX_OPEN_POSITIONS_REACHED, f"{open_count}/{policy.max_open_positions}")

    # Buy Amount fix: policy.buy_amount_sol is a ETH amount now (see
    # models/auto_trade_policy.py), entered directly by the user.
    buy_amount_sol = float(policy.buy_amount_sol or 0.0)
    if buy_amount_sol <= 0:
        return GateResult(False, RejectionReason.INVALID_TRADE_AMOUNT)
    # NOTE: max_position_size_usdt remains a USDT-denominated cap (a
    # separate, unrelated setting, out of scope for this fix) -- this
    # comparison no longer compares like units against buy_amount_sol.
    if policy.max_position_size_usdt is not None and buy_amount_sol > float(policy.max_position_size_usdt):
        return GateResult(False, RejectionReason.INVALID_TRADE_AMOUNT, "exceeds max_position_size_usdt")

    return GateResult(True)


async def evaluate_execution_risk(user_id: int, sol_amount: float) -> GateResult:
    """§7 -- pre-trade execution risk gate. Separate from AlphaPulse's
    token qualification: this only asks "is it currently executable",
    not "is this token good". Live wallet-balance check; token
    tradability/liquidity/route are re-verified naturally by the quote
    step in execution.py (a stale/untradeable token simply fails the
    quote), rather than duplicated here.
    """
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return GateResult(False, RejectionReason.NO_WALLET)

    try:
        balance_wei = int((await get_native_balance(wallet.public_key)) * 1_000_000_000_000_000_000)
    except SwapError as e:
        logger.warning("[AutoTrade] balance preflight failed user=%s: %s", user_id, e)
        return GateResult(False, RejectionReason.EXECUTION_UNAVAILABLE, "wallet balance check failed")
    except Exception as e:
        logger.error("[AutoTrade] unexpected balance preflight error user=%s: %s", user_id, e)
        return GateResult(False, RejectionReason.EXECUTION_UNAVAILABLE, "unexpected balance error")

    required_wei = int(sol_amount * 1_000_000_000_000_000_000) + BUY_NETWORK_RESERVE_WEI
    if balance_wei < required_wei:
        return GateResult(False, RejectionReason.INSUFFICIENT_BALANCE)

    return GateResult(True)
