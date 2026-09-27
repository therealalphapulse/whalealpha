"""domain/trading/auto_trade/orchestrator.py

§8 (Trade Orchestrator), §40 (Example Complete Lifecycle). Coordinates:
  Signal -> User Policy -> Pre-Trade Risk -> Idempotent Claim -> Buy ->
  Confirmation -> Position Creation -> Notify

Ordering is deliberate and load-bearing for correctness under
concurrency, same convention as
real_automation_engine._execute_auto_buy: the idempotency claim is
reserved FIRST (cheapest, and the only thing that stops two concurrent
evaluations of the same signal from both proceeding), then the daily
trade-slot count, then exposure, and only then the actual swap.

Failure handling (see claims.py's module docstring for the full
retry/pause policy this implements):
  - A deterministic rejection (daily limit, exposure cap, insufficient
    balance, no wallet, a definitively-failed buy) marks the claim
    "skipped" -- this exact signal_id is never retried, so it can
    never spam a repeat notification.
  - Insufficient balance / no wallet additionally engage a wallet-level
    pause (policy_service.pause_auto_trade) so a *different* signal
    arriving minutes later doesn't just trigger its own fresh
    rejection -- the whole wallet is skipped until the pause clears.
  - A transient/technical failure (EXECUTION_UNAVAILABLE, or a buy
    that failed for a reason that looks like an RPC/on-chain blip) is
    retried up to MAX_CLAIM_RETRY_ATTEMPTS times with real backoff
    between attempts, and the user is only notified once -- either on
    the eventual success, or once retries are exhausted -- never once
    per attempt.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from infra.db.session import async_session
from models.auto_trade_policy import AutoTradePolicy
from models.auto_trade_position import AutoTradePosition, AutoTradeState
from models.auto_trade_execution import AutoTradeExecution
from models.real_wallet import RealWallet

from . import claims, execution, policy_service, risk_gate
from .constants import MAX_CLAIM_RETRY_ATTEMPTS, RETRYABLE_REJECTION_REASONS, RejectionReason
from .signal_adapter import AutoTradeSignal, get_recent_qualifying_signals

logger = logging.getLogger("AlphaPulse.AutoTrade.Orchestrator")

# Reasons handed back by execution.execute_buy_swap (free-form strings,
# not RejectionReason codes -- see that module's "ok"/"uncertain"/
# "reason" contract) that reflect a permanent misconfiguration rather
# than a technical blip. Retrying these a few seconds later can't help,
# so they're excluded from the default "retryable" treatment every
# other definitive buy failure gets (on-chain rejections, quote
# errors, unexpected exceptions -- all technical, all worth a bounded
# retry, which is what a professional execution engine does instead of
# either hot-looping or giving up on the first blip).
_NON_RETRYABLE_BUY_FAILURES = ("No active wallet.", "Amount must be greater than 0 ETH.")


def _is_retryable_buy_failure(reason: str | None) -> bool:
    return reason not in _NON_RETRYABLE_BUY_FAILURES


async def _get_auto_trade_enabled_wallets() -> list[RealWallet]:
    """Users who both have an active RealWallet AND an
    auto_trade_enabled AutoTradePolicy. Reuses RealWallet (existing,
    shared infra) purely to know which users have a wallet/key material
    to sign with -- the enable flag and every trading rule live only in
    AutoTradePolicy, never in RealWallet's own (untouched)
    auto_trading_enabled column."""
    async with async_session() as session:
        result = await session.execute(
            select(RealWallet, AutoTradePolicy)
            .join(AutoTradePolicy, AutoTradePolicy.user_id == RealWallet.user_id)
            .where(
                RealWallet.is_active == True,  # noqa: E712
                AutoTradePolicy.auto_trade_enabled == True,  # noqa: E712
                AutoTradePolicy.kill_switch == False,  # noqa: E712
            )
        )
        return result.all()


async def _notify(bot, user_id: int, text: str) -> None:
    if bot is None:
        return
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning("[AutoTrade] could not notify user %s: %s", user_id, e)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def session_set_position_state(position_id: int, state: str, **fields) -> None:
    async with async_session() as session:
        position = await session.get(AutoTradePosition, position_id)
        if position is None:
            return
        position.state = state
        for key, value in fields.items():
            setattr(position, key, value)
        await session.commit()


async def try_auto_trade(bot, user_id: int, policy: AutoTradePolicy, signal: AutoTradeSignal) -> None:
    """Full authorize -> buy -> position pipeline for one (user, signal)
    pair. Never raises -- every failure path is handled and logged."""

    gate = await risk_gate.evaluate_user_policy(user_id, policy, signal)
    if not gate:
        logger.debug("[AutoTrade] rejected user=%s signal=%s reason=%s", user_id, signal.signal_id, gate.reason)
        return

    claim = await claims.try_claim(user_id, signal.signal_id, signal.contract)
    if claim is None:
        return  # already claimed/in-flight/committed/skipped/backed-off -- no duplicate trade or notification (§9)

    buy_slot = await policy_service.register_daily_trade(user_id, policy.daily_trade_limit)
    if not buy_slot["ok"]:
        await claims.finalize_claim(claim.id, retryable=False, reason=buy_slot["reason"])
        logger.info("[AutoTrade] daily limit reached user=%s: %s", user_id, buy_slot["reason"])
        return

    # Buy Amount fix: policy.buy_amount_sol is the ETH amount to spend,
    # entered directly by the user -- no market-price conversion needed
    # to size the swap (previously this divided a USDT figure by a
    # freshly-fetched ETH/USD price, which also silently skipped the
    # trade whenever that price lookup failed).
    sol_amount = float(policy.buy_amount_sol or 0.0)
    # NOTE: register_exposure/max_total_exposure_usdt remains a
    # USDT-denominated cap (a separate, unrelated setting, out of scope
    # for this fix) -- passing a ETH amount through it means that cap
    # no longer compares like units against what a user configures there.
    exposure_check = await policy_service.register_exposure(user_id, sol_amount, policy.max_total_exposure_usdt)
    if not exposure_check["ok"]:
        await policy_service.release_daily_trade(user_id)
        await claims.finalize_claim(claim.id, retryable=False, reason=exposure_check["reason"])
        logger.info("[AutoTrade] exposure cap reached user=%s: %s", user_id, exposure_check["reason"])
        return

    risk = await risk_gate.evaluate_execution_risk(user_id, sol_amount)
    if not risk:
        await policy_service.release_exposure(user_id, sol_amount)
        await policy_service.release_daily_trade(user_id)
        retryable = risk.reason in RETRYABLE_REJECTION_REASONS
        outcome = await claims.finalize_claim(claim.id, retryable=retryable, reason=risk.reason)
        logger.info("[AutoTrade] execution risk gate failed user=%s reason=%s outcome=%s", user_id, risk.reason, outcome)

        if outcome == "retry_scheduled":
            # Transient (e.g. a balance-check RPC call failing) -- retried
            # automatically with backoff, no notification for this attempt.
            return

        if risk.reason in (RejectionReason.INSUFFICIENT_BALANCE, RejectionReason.NO_WALLET):
            # Deterministic, account-level: won't resolve itself in the next
            # few minutes, and a different signal for a different token
            # would hit the exact same wall -- pause the whole wallet so it
            # only has to be reported once, not once per signal.
            await policy_service.pause_auto_trade(user_id, reason=risk.reason)
            human_reason = "Insufficient wallet balance" if risk.reason == RejectionReason.INSUFFICIENT_BALANCE else "No active wallet"
            await _notify(
                bot, user_id,
                f"⏸ <b>Auto-Trade Paused</b>\n"
                f"Token: {signal.symbol}\n"
                f"Reason: {human_reason}\n\n"
                f"Auto-buy attempts are paused for this wallet — fund it (or connect one) and it'll resume "
                f"automatically on the next check.",
            )
        elif outcome == "gave_up":
            await _notify(
                bot, user_id,
                f"⚠️ <b>Auto-Trade Rejected</b>\nToken: {signal.symbol}\nReason: {risk.reason}\n"
                f"Gave up after {MAX_CLAIM_RETRY_ATTEMPTS} attempts — this signal won't be retried again.",
            )
        else:
            await _notify(bot, user_id, f"⚠️ <b>Auto-Trade Rejected</b>\nToken: {signal.symbol}\nReason: {risk.reason}")
        return

    snapshot = policy_service.snapshot_policy(policy)

    async with async_session() as session:
        position = AutoTradePosition(
            user_id=user_id, signal_id=signal.signal_id, claim_id=claim.id,
            contract=signal.contract, name=signal.name, symbol=signal.symbol,
            state=AutoTradeState.BUY_SUBMITTED,
            policy_snapshot_json=snapshot.to_json(),
            # requested_usdt now holds the ETH amount requested (Buy Amount fix);
            # field kept for backward compatibility with existing rows/queries.
            signal_price=signal.price, requested_usdt=sol_amount,
        )
        session.add(position)
        await session.commit()
        await session.refresh(position)

    await _notify(
        bot, user_id,
        f"🤖 <b>Auto-Trade</b>\nStatus: BUYING\nToken: {signal.symbol}\nAmount: {sol_amount:.4f} ETH",
    )

    result = await execution.execute_buy_swap(
        user_id=user_id, contract=signal.contract, sol_amount=sol_amount,
        slippage_bps=snapshot.slippage_bps, priority_fee_tier=snapshot.priority_fee_tier,
    )

    if not result["ok"]:
        await policy_service.release_exposure(user_id, sol_amount)
        await policy_service.release_daily_trade(user_id)
        if result.get("uncertain"):
            # Genuinely unknown outcome (e.g. submitted but confirmation
            # timed out) -- leave the claim "pending" for reconciliation.py
            # to resolve once the real on-chain outcome is known; retrying
            # a fresh buy here would risk a double-spend.
            await session_set_position_state(position.id, AutoTradeState.BUY_RECONCILING, last_error=result["reason"])
            logger.warning("[AutoTrade] buy outcome uncertain user=%s contract=%s: %s", user_id, signal.contract, result["reason"])
            return

        retryable = _is_retryable_buy_failure(result["reason"])
        outcome = await claims.finalize_claim(claim.id, retryable=retryable, reason=result["reason"])
        await session_set_position_state(position.id, AutoTradeState.BUY_FAILED, last_error=result["reason"], closed_at=_now())
        logger.warning("[AutoTrade] buy failed user=%s contract=%s outcome=%s: %s", user_id, signal.contract, outcome, result["reason"])

        if outcome == "retry_scheduled":
            return  # technical failure, retried automatically with backoff -- no notification yet
        if outcome == "gave_up":
            await _notify(
                bot, user_id,
                f"⚠️ <b>Auto-Trade Buy Failed</b>\nToken: {signal.symbol}\nReason: {result['reason']}\n"
                f"Gave up after {MAX_CLAIM_RETRY_ATTEMPTS} attempts — this signal won't be retried again.",
            )
        else:
            await _notify(bot, user_id, f"⚠️ <b>Auto-Trade Buy Failed</b>\nToken: {signal.symbol}\nReason: {result['reason']}")
        return

    await claims.commit_claim(claim.id)
    entry_price = result.get("effective_entry_price") or signal.price
    token_quantity = result["token_quantity"]

    async with async_session() as session:
        db_position = await session.get(AutoTradePosition, position.id)
        db_position.state = AutoTradeState.POSITION_OPEN
        db_position.entry_price = entry_price
        db_position.token_quantity = token_quantity
        db_position.remaining_quantity = token_quantity
        db_position.token_decimals = result["decimals"]
        db_position.sol_spent = result["sol_spent"]
        db_position.total_cost_basis_sol = result["sol_spent"]
        db_position.buy_tx_signature = result["signature"]
        db_position.opened_at = _now()
        db_position.highest_observed_price = entry_price
        session.add(AutoTradeExecution(
            position_id=position.id, user_id=user_id, side="buy", contract=signal.contract,
            requested_amount=sol_amount, actual_amount=result["sol_spent"],
            requested_price=signal.price, actual_price=entry_price,
            quote_amount=token_quantity, transaction_signature=result["signature"],
            provider="jupiter", status="confirmed_success", confirmed_at=_now(),
        ))
        await session.commit()

    logger.info(
        "[AutoTrade][trade=%s] BUY_CONFIRMED user=%s contract=%s spent=%.4f ETH received=%.4f entry=%.8f",
        position.id, user_id, signal.contract, result["sol_spent"], token_quantity, entry_price or 0.0,
    )

    tp = policy.take_profit_pct
    sl = policy.stop_loss_pct
    await _notify(
        bot, user_id,
        "✅ <b>Auto-Trade Buy Confirmed</b>\n"
        f"Token: {signal.symbol}\n"
        f"Spent: {result['sol_spent']:.4f} ETH\n"
        f"Received: {token_quantity:,.2f} {signal.symbol}\n"
        f"Entry: {entry_price:.8f}\n"
        f"Transaction: CONFIRMED\n"
        f"TP: {f'+{tp:g}%' if tp else '—'} | SL: {f'-{sl:g}%' if sl else '—'}\n\n"
        "Manage this from /wallet.",
    )


async def scan_and_authorize(bot=None) -> int:
    """One tick of the scan-and-buy loop (§40). Returns the number of
    (user, signal) pairs evaluated -- not the number bought (most are
    expected to be rejected/skipped, which is normal, not an error)."""
    wallets_and_policies = await _get_auto_trade_enabled_wallets()
    if not wallets_and_policies:
        return 0
    signals = await get_recent_qualifying_signals()
    if not signals:
        return 0

    evaluated = 0
    for wallet, policy in wallets_and_policies:
        if policy_service.is_paused(policy):
            # Wallet-level circuit breaker engaged (e.g. insufficient
            # balance) -- skip every signal for this user without a
            # single risk-gate call or notification until it clears.
            logger.debug("[AutoTrade] skipping paused user=%s reason=%s until=%s", wallet.user_id, policy.paused_reason, policy.paused_until)
            continue
        for signal in signals:
            evaluated += 1
            try:
                await try_auto_trade(bot, wallet.user_id, policy, signal)
            except Exception as e:
                logger.error("[AutoTrade] unhandled error evaluating user=%s signal=%s: %s", wallet.user_id, signal.signal_id, e)
    return evaluated
