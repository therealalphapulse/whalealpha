"""
Real Wallet Exit Automation — Take Profit / Stop Loss / Partial Take
Profit engine, available to every user.

SCOPE OF THIS FILE
-------------------
Adds unattended exits on top of the existing Real Wallet position
model (models/real_trade.py) without touching it. Every rule lives in
its own table (models/real_exit_rule.py); every sell it fires goes
through services.real_trade_engine.execute_real_sell (fraction=...) —
the same primitive manual "Sell 25/50/75/100%" buttons use — so there
is exactly one place that ever signs/broadcasts a Real Wallet
transaction. That function atomically claims the position before
swapping, so a rule firing here can never race a manual sell (or
another rule) into a double-sell of the same position.

Does not read or modify the Signal Engine, AI Scoring, Wallet
Discovery Engine, Trending, Paper Trading, or any other completed
module — only RealTrade (read-only, for entry_price/remaining_quantity)
and its own RealExitRule table.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from infra.db.session import async_session
from models.real_exit_rule import RealExitRule
from models.real_trade import RealTrade
from providers.marketdata.dexscreener import get_token_card_info
from domain.trading.real.solana_wallet import get_wallet_settings
from domain.trading.real import real_trade_engine

logger = logging.getLogger("AlphaPulse.RealExitEngine")

VALID_KINDS = {"tp", "sl", "ptp"}


class ExitRuleValidationError(ValueError):
    """User-facing exit rule validation error."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _validate_rule(kind: str, trigger_pct: float, sell_fraction: float) -> None:
    if kind not in VALID_KINDS:
        raise ExitRuleValidationError("Unknown rule type.")
    if trigger_pct <= 0:
        raise ExitRuleValidationError("Trigger % must be greater than 0.")
    if not (0.0 < sell_fraction <= 1.0):
        raise ExitRuleValidationError("Sell fraction must be between 0% and 100%.")
    if kind in ("tp", "sl") and sell_fraction != 1.0:
        # Full TP/SL always close the position outright; use "ptp" for
        # a partial exit so the distinction stays visible in history.
        raise ExitRuleValidationError("Take Profit / Stop Loss always sell 100% — use Partial Take Profit for a partial exit.")


async def create_rule(
    user_id: int,
    trade_id: int,
    kind: str,
    trigger_pct: float,
    sell_fraction: float = 1.0,
) -> RealExitRule:
    _validate_rule(kind, trigger_pct, sell_fraction)

    async with async_session() as session:
        result = await session.execute(
            select(RealTrade).where(
                RealTrade.id == trade_id, RealTrade.user_id == user_id, RealTrade.status == "open"
            )
        )
        if not result.scalar_one_or_none():
            raise ExitRuleValidationError("Position not found or already closed.")

        rule = RealExitRule(
            user_id=user_id,
            trade_id=trade_id,
            kind=kind,
            trigger_pct=trigger_pct,
            sell_fraction=sell_fraction,
        )
        session.add(rule)
        await session.commit()
        await session.refresh(rule)
    return rule


async def get_rules_for_trade(user_id: int, trade_id: int) -> list[RealExitRule]:
    async with async_session() as session:
        result = await session.execute(
            select(RealExitRule)
            .where(RealExitRule.user_id == user_id, RealExitRule.trade_id == trade_id)
            .order_by(RealExitRule.created_at.asc())
        )
        return result.scalars().all()


async def cancel_rule(user_id: int, rule_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(RealExitRule).where(RealExitRule.id == rule_id, RealExitRule.user_id == user_id)
        )
        rule = result.scalar_one_or_none()
        if not rule or rule.status != "active":
            return False
        rule.status = "cancelled"
        await session.commit()
    return True


def _trigger_price(entry_price: float, kind: str, trigger_pct: float) -> float:
    if kind == "sl":
        return entry_price * (1 - trigger_pct / 100)
    return entry_price * (1 + trigger_pct / 100)  # "tp" and "ptp" both move up


def _condition_met(kind: str, current_price: float, target_price: float) -> bool:
    if kind == "sl":
        return current_price <= target_price
    return current_price >= target_price


async def _get_active_rules_by_trade() -> dict[int, list[RealExitRule]]:
    async with async_session() as session:
        result = await session.execute(
            select(RealExitRule).where(RealExitRule.status == "active")
        )
        rules = result.scalars().all()

    grouped: dict[int, list[RealExitRule]] = {}
    for r in rules:
        grouped.setdefault(r.trade_id, []).append(r)
    return grouped


async def _notify(bot, user_id: int, text: str) -> None:
    if bot is None:
        return
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Could not notify user {user_id} about an exit rule: {e}")


KIND_LABELS = {"tp": "🎯 Take Profit", "sl": "🛑 Stop Loss", "ptp": "🎯 Partial Take Profit"}

# Reasons execute_real_sell() can return that are structural/terminal for
# this position — no retry at any price or balance will ever change the
# outcome, so the rule is correctly marked "failed" for visibility and the
# user is told to act manually.
_TERMINAL_SELL_REASONS = frozenset({
    "No active Real Wallet. Use /realwallet to set one up.",
    "Trade not found or already closed.",
    "Nothing left to sell on this position.",
    "Sell amount rounds to zero at the token's native precision.",
})


async def _fire_rule(bot, rule: RealExitRule, trade: RealTrade, current_price: float) -> None:
    settings = await get_wallet_settings(rule.user_id)
    result = await real_trade_engine.execute_real_sell(
        user_id=rule.user_id,
        trade_id=trade.id,
        current_price=current_price,
        fraction=rule.sell_fraction,
        slippage_bps=settings["slippage_bps"],
        priority_fee_tier=settings["priority_fee_tier"],
    )

    # If a manual sell, Auto Trade, a limit order, or another rule was
    # already selling this exact position when this rule tried to fire,
    # leave the rule "active" so it re-evaluates on the next tick
    # instead of being marked permanently "failed" over a transient
    # timing conflict.
    if not result["ok"] and result["reason"] == real_trade_engine.SELL_ALREADY_IN_PROGRESS:
        return

    if not result["ok"] and result["reason"] not in _TERMINAL_SELL_REASONS:
        # The rule condition has already been met (price crossed the
        # trigger threshold) — that decision is final. Everything past
        # this point (on-chain balance verification, the Jupiter
        # quote/build/broadcast, an on-chain rejection because the price
        # kept moving and slippage was exceeded, or any other RPC/network
        # hiccup) is an *execution* concern, not a re-litigation of
        # whether the trade should happen. None of those are reasons to
        # silently and permanently disable a user's Take Profit / Stop
        # Loss: we keep the rule "active" so scan_and_execute() retries
        # it on the very next tick with a freshly re-verified on-chain
        # balance and the then-current market price — exactly as if the
        # rule had just triggered again.
        logger.warning(
            "Exit rule %s (trade=%s) fire attempt failed, will retry next tick: %s",
            rule.id, trade.id, result["reason"],
        )
        should_notify = False
        async with async_session() as session:
            result_row = await session.execute(select(RealExitRule).where(RealExitRule.id == rule.id))
            row = result_row.scalar_one_or_none()
            if row and row.status == "active":
                should_notify = row.last_error != result["reason"]
                row.last_error = result["reason"]
                await session.commit()
        if should_notify:
            await _notify(
                bot, rule.user_id,
                f"⚠️ {KIND_LABELS[rule.kind]} on {trade.symbol or trade.contract[:6]} triggered but the sell "
                f"hasn't gone through yet ({result['reason']}). Still watching this position and will keep "
                f"retrying automatically at the current market price and balance.",
            )
        return

    async with async_session() as session:
        result_row = await session.execute(select(RealExitRule).where(RealExitRule.id == rule.id))
        row = result_row.scalar_one_or_none()
        if not row:
            return
        if result["ok"]:
            row.status = "triggered"
            row.tx_signature = result["signature"]
            row.triggered_at = _now()
        else:
            row.status = "failed"
            row.last_error = result["reason"]
        await session.commit()

    if result["ok"]:
        await _notify(
            bot, rule.user_id,
            f"{KIND_LABELS[rule.kind]} triggered on {trade.symbol or trade.contract[:6]} at +{rule.trigger_pct if rule.kind != 'sl' else -rule.trigger_pct}% "
            f"from entry.\nSold {rule.sell_fraction * 100:.0f}% of the position.\n"
            f"Received: {result.get('sol_received', 0):.4f} SOL\n"
            f"Tx: <code>{result['signature']}</code>",
        )
    else:
        await _notify(
            bot, rule.user_id,
            f"⚠️ {KIND_LABELS[rule.kind]} on {trade.symbol or trade.contract[:6]} triggered but the sell failed: "
            f"{result['reason']}\n\nManage this position manually from /realwallet.",
        )


async def scan_and_execute(bot=None) -> int:
    """One exit-engine tick. Returns the number of rules fired (successfully or not)."""
    grouped = await _get_active_rules_by_trade()
    if not grouped:
        return 0

    fired = 0
    async with async_session() as session:
        result = await session.execute(
            select(RealTrade).where(RealTrade.id.in_(grouped.keys()), RealTrade.status == "open")
        )
        trades_by_id = {t.id: t for t in result.scalars().all()}

    # Group by contract so each distinct token's price is fetched once
    # per tick even if several users/rules watch the same token.
    price_cache: dict[str, float | None] = {}

    for trade_id, rules in grouped.items():
        trade = trades_by_id.get(trade_id)
        if not trade:
            continue  # position already closed manually — rules simply go stale, no action needed

        active_user_rules = [r for r in rules if r.status == "active"]
        if not active_user_rules:
            continue

        if trade.contract not in price_cache:
            price = None
            try:
                info = await get_token_card_info(trade.contract)
                if info and info.get("price") not in (None, "N/A"):
                    price = float(info["price"])
            except Exception:
                pass
            price_cache[trade.contract] = price

        current_price = price_cache[trade.contract]
        if current_price is None or not trade.entry_price:
            continue

        for rule in active_user_rules:
            target = _trigger_price(trade.entry_price, rule.kind, rule.trigger_pct)
            if _condition_met(rule.kind, current_price, target):
                try:
                    await _fire_rule(bot, rule, trade, current_price)
                    fired += 1
                except Exception as e:
                    logger.error(f"Exit rule {rule.id} failed to fire: {e}")

    return fired


async def real_exit_engine_loop(bot=None, interval_seconds: int = 20):
    """Background task registered in main.py — same shape as
    services.real_automation_engine.real_automation_loop."""
    logger.info("🎯 Real Wallet Exit Engine (TP/SL/Partial TP) starting...")
    while True:
        try:
            await scan_and_execute(bot=bot)
        except Exception as e:
            logger.error(f"Real exit engine loop error (non-fatal): {e}")
        await asyncio.sleep(interval_seconds)
