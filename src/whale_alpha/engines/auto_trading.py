"""Auto-trading orchestration — port of src/engines/autoTrading/autoTradingEngine.ts.

Given one qualified Signal, evaluates and (if approved) executes an auto-trade
for each eligible user. Auto Trading NEVER fires directly off a raw wallet-buy
event — only off a Signal that already passed the signal engine's confidence
threshold. This function is the second and final gate: per-user risk rules.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from whale_alpha.config import Env
from whale_alpha.db.models import Trade, TradeSide, TradeSource, TradeStatus
from whale_alpha.engines.risk import AutoTradingRules, UserTradingState, evaluate_auto_trade
from whale_alpha.engines.signal import SignalCandidate
from whale_alpha.engines.trade_executor import ExecuteTradeParams, execute_trade
from whale_alpha.utils.logger import child_logger

log = child_logger("autoTradingEngine")


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
) -> list[AutoTradeOutcome]:
    outcomes: list[AutoTradeOutcome] = []

    for user in users:
        check = evaluate_auto_trade(signal, market_cap_usd, liquidity_usd, user.rules, user.state)

        if not check.approved:
            outcomes.append(AutoTradeOutcome(user_id=user.user_id, approved=False, reasons=check.reasons))
            continue

        # TODO(integration): convert USD -> lamports using a live SOL/USD price
        # feed, exactly as flagged in the original TS TODO. Placeholder divisor
        # of 1 preserved intentionally so behavior matches the original until a
        # real price feed is wired in (see PORTING_NOTES.md).
        lamports = round((check.proposed_trade_usd / 1) * 1e9)

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
