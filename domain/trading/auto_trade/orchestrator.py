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
from .signal_adapter import AutoTradeSignal, get_recent_qualifying_signals

logger = logging.getLogger("AlphaPulse.AutoTrade.Orchestrator")


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
        return  # already claimed/in-flight/committed -- no duplicate trade (§9)

    buy_slot = await policy_service.register_daily_trade(user_id, policy.daily_trade_limit)
    if not buy_slot["ok"]:
        await claims.release_claim(claim.id)
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
        await claims.release_claim(claim.id)
        logger.info("[AutoTrade] exposure cap reached user=%s: %s", user_id, exposure_check["reason"])
        return

    risk = await risk_gate.evaluate_execution_risk(user_id, sol_amount)
    if not risk:
        await policy_service.release_exposure(user_id, sol_amount)
        await policy_service.release_daily_trade(user_id)
        await claims.release_claim(claim.id)
        logger.info("[AutoTrade] execution risk gate failed user=%s: %s", user_id, risk.reason)
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
            await session_set_position_state(position.id, AutoTradeState.BUY_RECONCILING, last_error=result["reason"])
            logger.warning("[AutoTrade] buy outcome uncertain user=%s contract=%s: %s", user_id, signal.contract, result["reason"])
        else:
            await claims.release_claim(claim.id)
            await session_set_position_state(position.id, AutoTradeState.BUY_FAILED, last_error=result["reason"], closed_at=_now())
            logger.warning("[AutoTrade] buy failed user=%s contract=%s: %s", user_id, signal.contract, result["reason"])
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
        for signal in signals:
            evaluated += 1
            try:
                await try_auto_trade(bot, wallet.user_id, policy, signal)
            except Exception as e:
                logger.error("[AutoTrade] unhandled error evaluating user=%s signal=%s: %s", wallet.user_id, signal.signal_id, e)
    return evaluated
