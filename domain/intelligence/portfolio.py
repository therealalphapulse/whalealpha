import html
from sqlalchemy import select, delete

from infra.db.session import async_session
from models.portfolio import PortfolioPosition
from providers.marketdata.dexscreener import get_token_card_info


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else "N/A"


def format_usd(value) -> str:
    try:
        num = float(value)

        if num >= 1_000_000_000:
            return f"${num / 1_000_000_000:.2f}B"
        elif num >= 1_000_000:
            return f"${num / 1_000_000:.2f}M"
        elif num >= 1_000:
            return f"${num / 1_000:.2f}K"
        else:
            return f"${num:,.2f}"
    except (ValueError, TypeError):
        return "N/A"


def format_price(value) -> str:
    try:
        num = float(value)

        if num >= 1:
            return f"${num:,.4f}"
        elif num >= 0.000001:
            return f"${num:.8f}".rstrip("0").rstrip(".")
        else:
            return f"${num:.12f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return "N/A"


def format_token_amount(value) -> str:
    try:
        num = float(value)

        if num >= 1_000_000_000:
            return f"{num / 1_000_000_000:.2f}B"
        elif num >= 1_000_000:
            return f"{num / 1_000_000:.2f}M"
        elif num >= 1_000:
            return f"{num / 1_000:.2f}K"
        else:
            return f"{num:,.4f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return "N/A"


async def add_or_update_position(
    user_id: int,
    contract: str,
    token_amount: float,
    entry_price: float | None = None,
) -> dict:
    """
    Add a new portfolio position or increase an existing one.

    If entry_price is not provided, current DexScreener price is used.
    If user already owns the token, we calculate a weighted average entry price.
    """

    token_data = await get_token_card_info(contract)

    if not token_data:
        return {
            "ok": False,
            "reason": "not_found",
            "message": "Token not found on DexScreener.",
        }

    current_price = _to_float(token_data.get("price"))

    if entry_price is None:
        entry_price = current_price

    if token_amount <= 0:
        return {
            "ok": False,
            "reason": "invalid_amount",
            "message": "Token amount must be greater than zero.",
        }

    if entry_price <= 0:
        return {
            "ok": False,
            "reason": "invalid_price",
            "message": "Entry price must be greater than zero.",
        }

    async with async_session() as session:
        result = await session.execute(
            select(PortfolioPosition).where(
                PortfolioPosition.user_id == user_id,
                PortfolioPosition.contract == contract,
            )
        )

        position = result.scalar_one_or_none()

        if position:
            old_amount = position.token_amount
            old_entry = position.entry_price

            old_cost = old_amount * old_entry
            new_cost = token_amount * entry_price

            total_amount = old_amount + token_amount
            weighted_entry = (old_cost + new_cost) / total_amount

            position.token_amount = total_amount
            position.entry_price = weighted_entry
            position.token_name = token_data.get("name", position.token_name)
            position.token_symbol = token_data.get("symbol", position.token_symbol)

            action = "updated"

        else:
            position = PortfolioPosition(
                user_id=user_id,
                contract=contract,
                token_name=token_data.get("name"),
                token_symbol=token_data.get("symbol"),
                token_amount=token_amount,
                entry_price=entry_price,
            )

            session.add(position)
            action = "created"

        await session.commit()

    return {
        "ok": True,
        "action": action,
        "name": token_data.get("name", "Unknown"),
        "symbol": token_data.get("symbol", "???"),
        "amount": token_amount,
        "entry_price": entry_price,
        "current_price": current_price,
    }


async def remove_position(user_id: int, contract: str) -> bool:
    """
    Remove a token from user portfolio.
    """

    async with async_session() as session:
        result = await session.execute(
            delete(PortfolioPosition).where(
                PortfolioPosition.user_id == user_id,
                PortfolioPosition.contract == contract,
            )
        )

        await session.commit()
        return result.rowcount > 0


async def get_user_positions(user_id: int) -> list[PortfolioPosition]:
    """
    Get all positions for a user.
    """

    async with async_session() as session:
        result = await session.execute(
            select(PortfolioPosition)
            .where(PortfolioPosition.user_id == user_id)
            .order_by(PortfolioPosition.created_at.desc())
        )

        return result.scalars().all()


async def build_portfolio_report(user_id: int) -> str:
    """
    Build a Telegram portfolio report with live DexScreener prices.
    """

    positions = await get_user_positions(user_id)

    if not positions:
        return (
            "📭 <b>Your Portfolio is Empty</b>\n\n"
            "Add your first position:\n"
            "<code>/portfolio_add &lt;contract&gt; &lt;token_amount&gt;</code>\n\n"
            "Example:\n"
            "<code>/portfolio_add DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 1000000</code>"
        )

    total_cost = 0.0
    total_value = 0.0

    text = (
        "💼 <b>AlphaPulse Portfolio</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    for index, position in enumerate(positions[:15], 1):
        token_data = await get_token_card_info(position.contract)

        name = position.token_name or "Unknown"
        symbol = position.token_symbol or "???"

        amount = position.token_amount
        entry_price = position.entry_price
        cost_basis = amount * entry_price

        current_price = 0.0
        current_value = 0.0
        pnl = 0.0
        pnl_pct = 0.0

        if token_data:
            name = token_data.get("name", name)
            symbol = token_data.get("symbol", symbol)
            current_price = _to_float(token_data.get("price"))
            current_value = amount * current_price

            if cost_basis > 0:
                pnl = current_value - cost_basis
                pnl_pct = (pnl / cost_basis) * 100

            total_cost += cost_basis
            total_value += current_value

            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            pnl_sign = "+" if pnl >= 0 else ""

            text += (
                f"<b>{index}. {_esc(name)} ({_esc(symbol)})</b>\n"
                f"   🪙 Amount: <b>{format_token_amount(amount)}</b>\n"
                f"   🎯 Entry: <b>{format_price(entry_price)}</b>\n"
                f"   💵 Current: <b>{format_price(current_price)}</b>\n"
                f"   💰 Cost: <b>{format_usd(cost_basis)}</b>\n"
                f"   💎 Value: <b>{format_usd(current_value)}</b>\n"
                f"   {pnl_emoji} PnL: <b>{pnl_sign}{format_usd(pnl)}</b> "
                f"(<b>{pnl_sign}{pnl_pct:.2f}%</b>)\n\n"
            )

        else:
            total_cost += cost_basis

            text += (
                f"<b>{index}. {_esc(name)} ({_esc(symbol)})</b>\n"
                f"   🪙 Amount: <b>{format_token_amount(amount)}</b>\n"
                f"   🎯 Entry: <b>{format_price(entry_price)}</b>\n"
                f"   ⚠️ Current price unavailable\n\n"
            )

    total_pnl = total_value - total_cost

    if total_cost > 0:
        total_pnl_pct = (total_pnl / total_cost) * 100
    else:
        total_pnl_pct = 0.0

    total_emoji = "🟢" if total_pnl >= 0 else "🔴"
    total_sign = "+" if total_pnl >= 0 else ""

    text += (
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Total Cost: <b>{format_usd(total_cost)}</b>\n"
        f"💎 Current Value: <b>{format_usd(total_value)}</b>\n"
        f"{total_emoji} Total PnL: <b>{total_sign}{format_usd(total_pnl)}</b> "
        f"(<b>{total_sign}{total_pnl_pct:.2f}%</b>)\n\n"
        "⚠️ <i>Manual portfolio tracking. Not financial advice.</i>\n"
        "⚡ Powered by AlphaPulse"
    )

    if len(positions) > 15:
        text += f"\n\nShowing first 15 of {len(positions)} positions."

    return text
