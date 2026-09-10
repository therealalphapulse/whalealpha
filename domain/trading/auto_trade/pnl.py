"""domain/trading/auto_trade/pnl.py

§26 (P&L Calculation Rules) and §27 (P&L Reporting). Realized P&L is
always derived from the AutoTradeExecution ledger + AutoTradePosition's
running totals -- never recomputed from theoretical/signal prices.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from infra.db.session import async_session
from models.auto_trade_position import AutoTradePosition, AutoTradeState


@dataclass(frozen=True)
class PnlSummary:
    realized_pnl_sol: float
    realized_pnl_pct: float | None
    total_cost_basis_sol: float
    total_proceeds_sol: float
    closed_trade_count: int
    win_count: int
    loss_count: int
    win_rate_pct: float | None


async def get_realized_pnl_summary(user_id: int) -> PnlSummary:
    async with async_session() as session:
        result = await session.execute(
            select(AutoTradePosition).where(
                AutoTradePosition.user_id == user_id,
                AutoTradePosition.state.in_({AutoTradeState.CLOSED, AutoTradeState.PARTIALLY_CLOSED}),
            )
        )
        positions = result.scalars().all()

    if not positions:
        return PnlSummary(0.0, None, 0.0, 0.0, 0, 0, 0, None)

    total_cost = sum(float(p.total_cost_basis_sol or 0.0) for p in positions)
    total_proceeds = sum(float(p.realized_proceeds_sol or 0.0) for p in positions)
    realized_pnl = sum(float(p.realized_pnl_sol or 0.0) for p in positions)
    closed = [p for p in positions if p.state == AutoTradeState.CLOSED]
    wins = sum(1 for p in closed if float(p.realized_pnl_sol or 0.0) > 0)
    losses = sum(1 for p in closed if float(p.realized_pnl_sol or 0.0) <= 0)
    win_rate = (wins / len(closed) * 100.0) if closed else None
    pnl_pct = (realized_pnl / total_cost * 100.0) if total_cost > 0 else None

    return PnlSummary(
        realized_pnl_sol=realized_pnl, realized_pnl_pct=pnl_pct,
        total_cost_basis_sol=total_cost, total_proceeds_sol=total_proceeds,
        closed_trade_count=len(closed), win_count=wins, loss_count=losses, win_rate_pct=win_rate,
    )
