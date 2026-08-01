"""Auto-trading orchestration — port of src/engines/autoTrading/autoTradingEngine.ts.

Given one qualified Signal, evaluates and (if approved) executes an auto-trade
for each eligible user. Auto Trading NEVER fires directly off a raw wallet-buy
event — only off a Signal that already passed the signal engine's confidence
threshold. This function is the second and final gate: per-user risk rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time

import httpx
from solana.rpc.async_api import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from whale_alpha.config import Env
from whale_alpha.db.models import AutoTradingConfig, Trade, TradeSide, TradeSource, TradeStatus, User
from whale_alpha.engines.risk import AutoTradingRules, UserTradingState, evaluate_auto_trade
from whale_alpha.engines.signal import SignalCandidate
from whale_alpha.engines.trade_executor import ExecuteTradeParams, execute_trade
from whale_alpha.integrations.solana_connection import get_sol_balance
from whale_alpha.utils.logger import child_logger

log = child_logger("autoTradingEngine")

# Statuses that count towards a user's daily trade/exposure counters and
# "there's an open position" checks. A trade that never landed (FAILED) or
# was never submitted (CANCELLED) shouldn't count against the user's limits.
_LIVE_TRADE_STATUSES = (TradeStatus.PENDING, TradeStatus.SUBMITTED, TradeStatus.CONFIRMED)


@dataclass
class EligibleUser:
    user_id: str
    encrypted_wallet_key: str
    rules: AutoTradingRules
    state: UserTradingState


@dataclass
class AutoTradeOutcome:
    user_id: str
    approved: bool
    reasons: list[str]
    tx_signature: str | None = None
    error: str | None = None


async def process_signal_for_auto_trading(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    env: Env,
    signal: SignalCandidate,
    signal_id: str,
    market_cap_usd: float | None,
    liquidity_usd: float | None,
    users: list[EligibleUser],
    sol_price_usd: float,
) -> list[AutoTradeOutcome]:
    outcomes: list[AutoTradeOutcome] = []

    for user in users:
        check = evaluate_auto_trade(signal, market_cap_usd, liquidity_usd, user.rules, user.state)

        if not check.approved:
            outcomes.append(AutoTradeOutcome(user_id=user.user_id, approved=False, reasons=check.reasons))
            continue

        # FIXED (was a hardcoded `/ 1` placeholder — see PORTING_NOTES.md and
        # integrations/price_feed.py): convert USD -> lamports using the live
        # SOL/USD price the caller fetched for this evaluation cycle.
        lamports = round((check.proposed_trade_usd / sol_price_usd) * 1e9)

        # --- NEW (porting requirement #3): create the PENDING Trade row BEFORE
        # calling the executor, so a crash before/during submission still has a
        # durable record for the startup reconciliation sweep to find.
        trade = Trade(
            user_id=user.user_id,
            signal_id=signal_id,
            source=TradeSource.AUTO_SIGNAL,
            side=TradeSide.BUY,
            token_mint=signal.token_mint,
            amount_usd=check.proposed_trade_usd,
            status=TradeStatus.PENDING,
            slippage_bps=user.rules.max_slippage_bps,
        )
        session.add(trade)
        await session.commit()
        await session.refresh(trade)

        try:
            result = await execute_trade(
                session,
                http_client,
                env,
                ExecuteTradeParams(
                    side="BUY",
                    token_mint=signal.token_mint,
                    amount_lamports_or_tokens=lamports,
                    slippage_bps=user.rules.max_slippage_bps,
                    encrypted_wallet_key=user.encrypted_wallet_key,
                    trade_row_id=trade.id,
                ),
            )
            outcomes.append(
                AutoTradeOutcome(
                    user_id=user.user_id,
                    approved=True,
                    reasons=[],
                    tx_signature=result.tx_signature,
                )
            )
        except Exception as err:  # noqa: BLE001 — mirrors the TS catch-all
            log.error(
                "Auto-trade execution failed",
                err=str(err),
                user_id=user.user_id,
                token_mint=signal.token_mint,
            )
            outcomes.append(
                AutoTradeOutcome(
                    user_id=user.user_id,
                    approved=False,
                    reasons=["EXECUTION_FAILED"],
                    error=str(err),
                )
            )

    return outcomes


async def build_eligible_users(
    session: AsyncSession,
    solana_connection: AsyncClient,
    sol_price_usd: float,
) -> list[EligibleUser]:
    """Assembles the EligibleUser list the scheduler feeds into
    `process_signal_for_auto_trading`. This is NEW — the original TODO only
    said "call process_signal_for_auto_trading for eligible users" without
    specifying how to build that list, so this is a judgment call, flagged
    the same way PORTING_NOTES.md flags others:

    * Eligible = has a connected wallet, has an AutoTradingConfig row, and
      `enabled=True`. Nothing here re-checks the risk engine — that still
      happens per-user inside `process_signal_for_auto_trading` via
      `evaluate_auto_trade`; this only decides who's even a candidate.
    * `state.portfolio_value_usd` is approximated as the user's live on-chain
      SOL balance * sol_price_usd. It intentionally does NOT include the
      current USD value of any open token positions (that would require a
      live per-token price lookup and a cost-basis model this schema doesn't
      track yet) — so `percent_allocation` sizing is conservative for anyone
      holding open positions, not exact. Fine for `fixed_trade_amount_usd`
      sizing, which ignores portfolio_value_usd entirely.
    * `open_positions` counts distinct token mints where the user has more
      CONFIRMED buys than CONFIRMED sells — an approximation of "still
      holding some", not an exact position tracker (no per-position
      cost-basis/quantity table exists in this schema).
    """
    result = await session.execute(
        select(User)
        .options(selectinload(User.auto_trading_config))
        .join(AutoTradingConfig, AutoTradingConfig.user_id == User.id)
        .where(
            AutoTradingConfig.enabled.is_(True),
            User.encrypted_wallet_key.is_not(None),
            User.wallet_public_key.is_not(None),
        )
    )
    users = list(result.scalars().unique())

    eligible: list[EligibleUser] = []
    for user in users:
        cfg = user.auto_trading_config
        if cfg is None:  # defensive; the JOIN above guarantees a row exists
            continue

        state = await _load_trading_state(session, solana_connection, user, sol_price_usd)

        rules = AutoTradingRules(
            enabled=cfg.enabled,
            max_slippage_bps=cfg.max_slippage_bps,
            min_liquidity_usd=cfg.min_liquidity_usd,
            max_open_positions=cfg.max_open_positions,
            max_daily_trades=cfg.max_daily_trades,
            max_daily_exposure_usd=cfg.max_daily_exposure_usd,
            cooldown_minutes=cfg.cooldown_minutes,
            fixed_trade_amount_usd=cfg.fixed_trade_amount_usd,
            percent_allocation=cfg.percent_allocation,
            max_market_cap_usd=cfg.max_market_cap_usd,
            token_blacklist=cfg.token_blacklist,
        )

        eligible.append(
            EligibleUser(
                user_id=user.id,
                encrypted_wallet_key=user.encrypted_wallet_key,  # type: ignore[arg-type]
                rules=rules,
                state=state,
            )
        )

    return eligible


async def _load_trading_state(
    session: AsyncSession,
    solana_connection: AsyncClient,
    user: User,
    sol_price_usd: float,
) -> UserTradingState:
    today_start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)

    todays_trades_result = await session.execute(
        select(Trade).where(
            Trade.user_id == user.id,
            Trade.created_at >= today_start,
            Trade.status.in_(_LIVE_TRADE_STATUSES),
        )
    )
    todays_trades = list(todays_trades_result.scalars())
    trades_today = len(todays_trades)
    exposure_usd_today = sum(t.amount_usd for t in todays_trades)

    last_trade_result = await session.execute(
        select(Trade)
        .where(Trade.user_id == user.id, Trade.status.in_(_LIVE_TRADE_STATUSES))
        .order_by(Trade.created_at.desc())
        .limit(1)
    )
    last_trade = last_trade_result.scalar_one_or_none()
    last_trade_at = last_trade.created_at if last_trade else None

    confirmed_result = await session.execute(
        select(Trade.token_mint, Trade.side).where(
            Trade.user_id == user.id, Trade.status == TradeStatus.CONFIRMED
        )
    )
    buy_counts: dict[str, int] = {}
    sell_counts: dict[str, int] = {}
    for token_mint, side in confirmed_result.all():
        if side == TradeSide.BUY:
            buy_counts[token_mint] = buy_counts.get(token_mint, 0) + 1
        else:
            sell_counts[token_mint] = sell_counts.get(token_mint, 0) + 1
    open_positions = sum(1 for mint, buys in buy_counts.items() if buys > sell_counts.get(mint, 0))

    portfolio_value_usd = 0.0
    if user.wallet_public_key:
        try:
            sol_balance = await get_sol_balance(solana_connection, user.wallet_public_key)
            portfolio_value_usd = sol_balance * sol_price_usd
        except Exception as err:  # noqa: BLE001 — a balance-lookup failure shouldn't block evaluation
            log.warning("Failed to fetch SOL balance for portfolio sizing", user_id=user.id, err=str(err))

    return UserTradingState(
        open_positions=open_positions,
        trades_today=trades_today,
        exposure_usd_today=exposure_usd_today,
        last_trade_at=last_trade_at,
        portfolio_value_usd=portfolio_value_usd,
    )
