"""domain/trading/auto_trade/position_manager.py

§14/§15/§16/§22/§27. Read/update helpers for AutoTradePosition. This is
the only module (besides orchestrator.py/exit_engine.py) that writes to
auto_trade_positions.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, func as sa_func

from infra.db.session import async_session
from models.auto_trade_position import AutoTradePosition, AutoTradeState
from domain.trading.real.robinhood_swap import get_token_balance, SwapError, NATIVE_ETH_ADDRESS
from domain.trading.real.robinhood_wallet import get_real_wallet
from providers.marketdata.dexscreener import get_token_card_info

logger = logging.getLogger("AlphaPulse.AutoTrade.PositionManager")


async def _get_sol_usd_price() -> float | None:
    """ETH/USD price, used only to convert DexScreener's USD-denominated
    token price into the ETH-per-token domain that entry_price is
    stored in (orchestrator.try_auto_trade sets entry_price from the
    actual on-chain buy fill: sol_amount / token_quantity). Every
    caller of get_live_positions_view (TP/SL/trailing trigger
    evaluation in exit_engine.py, and the /wallet live PnL display)
    compares current_price against entry_price directly, so the two
    MUST be in the same unit -- comparing a USD/token figure against a
    ETH/token figure silently inflates the computed % change by
    roughly the ETH/USD rate and can fire a false "tp"/"sl"/"trailing"
    trigger after no real token-price movement at all. Returns None on
    any failure; never a guessed price."""
    try:
        info = await get_token_card_info(NATIVE_ETH_ADDRESS)
        raw_price = info.get("price") if info else None
        price = float(raw_price)
        if price > 0:
            return price
    except Exception as exc:
        logger.warning("[AutoTrade] unable to resolve ETH/USD price for exit pricing: %s", exc)
    return None


async def get_open_positions(user_id: int) -> list[AutoTradePosition]:
    async with async_session() as session:
        result = await session.execute(
            select(AutoTradePosition)
            .where(AutoTradePosition.user_id == user_id, AutoTradePosition.state.in_(AutoTradeState.OPEN_LIKE))
            .order_by(AutoTradePosition.opened_at.desc())
        )
        return result.scalars().all()


async def count_open_positions(user_id: int) -> int:
    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count(AutoTradePosition.id)).where(
                AutoTradePosition.user_id == user_id, AutoTradePosition.state.in_(AutoTradeState.OPEN_LIKE)
            )
        )
        return int(result.scalar_one() or 0)


async def has_open_position_for_contract(user_id: int, contract: str) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(AutoTradePosition.id).where(
                AutoTradePosition.user_id == user_id,
                AutoTradePosition.contract == contract,
                AutoTradePosition.state.in_(AutoTradeState.OPEN_LIKE),
            )
        )
        return result.first() is not None


async def get_last_trade_at(user_id: int):
    """Most recent AutoTradePosition.created_at for this user, across
    every state (not just open positions) -- used by risk_gate.py's
    cooldown_seconds check (§6 COOLDOWN_ACTIVE). created_at is set the
    moment a trade attempt is authorized (orchestrator.try_auto_trade),
    before the buy even executes, so this reflects "time of last trade
    attempt" regardless of whether that attempt ultimately succeeded,
    failed, or is still in flight -- which is the right basis for a
    cooldown between consecutive attempts, not just successful ones.
    Returns None if this user has never had a trade attempt."""
    async with async_session() as session:
        result = await session.execute(
            select(sa_func.max(AutoTradePosition.created_at)).where(AutoTradePosition.user_id == user_id)
        )
        return result.scalar_one_or_none()


async def total_open_cost_basis_sol(user_id: int) -> float:
    positions = await get_open_positions(user_id)
    return sum(float(p.total_cost_basis_sol or 0.0) for p in positions)


async def get_all_non_terminal_positions() -> list[AutoTradePosition]:
    """Used by reconciliation.py at worker startup (§33 Crash Recovery)."""
    async with async_session() as session:
        result = await session.execute(
            select(AutoTradePosition).where(AutoTradePosition.state.in_(AutoTradeState.NON_TERMINAL))
        )
        return result.scalars().all()


async def get_recent_history(user_id: int, limit: int = 20) -> list[AutoTradePosition]:
    async with async_session() as session:
        result = await session.execute(
            select(AutoTradePosition)
            .where(AutoTradePosition.user_id == user_id, AutoTradePosition.state == AutoTradeState.CLOSED)
            .order_by(AutoTradePosition.closed_at.desc())
            .limit(limit)
        )
        return result.scalars().all()


async def set_state(position_id: int, state: str, **fields) -> None:
    async with async_session() as session:
        position = await session.get(AutoTradePosition, position_id)
        if position is None:
            return
        position.state = state
        for key, value in fields.items():
            setattr(position, key, value)
        await session.commit()


async def get_live_positions_view(user_id: int) -> list[dict]:
    """Live price/PnL view for open positions -- used by exit_engine.py
    and the /wallet command. Mirrors real_trade_engine.get_real_positions_view's
    shape/approach (price refresh with graceful fallback on provider error)."""
    positions = await get_open_positions(user_id)
    if not positions:
        return []

    async def _with_price(position: AutoTradePosition) -> tuple[AutoTradePosition, float, bool]:
        try:
            info = await get_token_card_info(position.contract)
            raw_price_usd = info.get("price") if info else None
            price_usd = float(raw_price_usd)
            if price_usd > 0:
                sol_usd = await _get_sol_usd_price()
                if sol_usd and sol_usd > 0:
                    # Convert DexScreener's USD-per-token price into the
                    # ETH-per-token domain entry_price is stored in --
                    # see _get_sol_usd_price's docstring. Without this
                    # conversion, current_price (USD/token) and
                    # entry_price (ETH/token) are not comparable -- that
                    # unit mismatch was the root cause of false
                    # Take-Profit triggers (and a broken /wallet PnL
                    # display). If the ETH/USD price is unavailable, fall
                    # through to the stale-price fallback below rather
                    # than compare mismatched units.
                    price_sol = price_usd / sol_usd
                    return position, price_sol, False
        except Exception as exc:
            logger.warning("[AutoTrade] price refresh failed user=%s pos=%s: %s", user_id, position.id, exc)
        fallback = float(position.entry_price or 0.0)
        return position, fallback, True

    refreshed = await asyncio.gather(*(_with_price(p) for p in positions))
    view: list[dict] = []
    for position, current_price, price_stale in refreshed:
        entry_price = float(position.entry_price or 0.0)
        remaining_quantity = float(position.remaining_quantity or 0.0)
        cost_basis = float(position.total_cost_basis_sol or 0.0)
        entry_value_sol = cost_basis
        current_value_sol = entry_value_sol * (current_price / entry_price) if entry_price > 0 and entry_value_sol > 0 else 0.0
        unrealized_pnl_sol = current_value_sol - entry_value_sol
        roi_pct = (unrealized_pnl_sol / entry_value_sol * 100.0) if entry_value_sol > 0 else 0.0
        view.append({
            "position": position,
            "entry_price": entry_price,
            "current_price": current_price,
            "remaining_quantity": remaining_quantity,
            "current_value_sol": current_value_sol,
            "unrealized_pnl_sol": unrealized_pnl_sol,
            "roi_pct": roi_pct,
            "price_stale": price_stale,
        })
    return view


async def resolve_sellable_balance(user_id: int, position: AutoTradePosition) -> dict:
    """§22 -- reconcile the DB's remaining_quantity against the actual
    on-chain token balance before selling. Never assumes zero on a
    transient RPC failure; the caller is responsible for treating a
    failed lookup as SELL_BALANCE_REETHVING (retry), not as "no
    balance"."""
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return {"ok": False, "reason": "No active wallet."}
    try:
        balance = await get_token_balance(wallet.public_key, position.contract)
    except SwapError as e:
        logger.warning("[AutoTrade] sellable balance lookup failed user=%s pos=%s: %s", user_id, position.id, e)
        return {"ok": False, "reason": str(e), "transient": True}
    except Exception as e:
        logger.error("[AutoTrade] unexpected sellable balance error user=%s pos=%s: %s", user_id, position.id, e)
        return {"ok": False, "reason": "Unexpected error checking token balance.", "transient": True}

    onchain_amount = float(balance.get("ui_amount", 0.0) or 0.0)
    db_amount = float(position.remaining_quantity or 0.0)
    sellable = min(onchain_amount, db_amount) if db_amount > 0 else onchain_amount
    mismatch = abs(onchain_amount - db_amount) > max(1e-9, db_amount * 0.01)
    return {
        "ok": True,
        "onchain_amount": onchain_amount,
        "db_amount": db_amount,
        "sellable_amount": max(0.0, sellable),
        "mismatch": mismatch,
    }
