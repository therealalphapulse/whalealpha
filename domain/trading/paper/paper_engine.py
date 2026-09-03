import asyncio
import calendar
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func as sa_func, text

from infra.db.session import async_session, engine
from models.paper_portfolio import PaperPortfolio
from models.paper_trade import PaperTrade
from models.paper_settings import PaperSettings
from models.paper_autobuy_filter import PaperAutoBuyFilter
from models.paper_pnl_event import PaperPnlEvent
from models.paper_dca_settings import PaperDCASettings
from models.paper_dca_fill import PaperDcaFill
from models.system_flag import SystemFlag

logger = logging.getLogger("AlphaPulse.PaperEngine")


async def reset_stale_default_balances_once():
    """
    One-time fix for portfolios created before the default starting balance
    changed from $10,000 to $100. Only touches portfolios that are still
    exactly at the OLD default (balance == initial_balance == 10000), i.e.
    accounts that never traded or adjusted — anyone with real trading
    history or a different balance is left untouched, so no one's actual
    portfolio growth/history is altered. Guarded by a SystemFlag so this
    only ever runs once, even across restarts/redeploys.
    """
    flag_key = "paper_balance_reset_10000_to_100_v1"

    async with async_session() as session:
        result = await session.execute(select(SystemFlag).where(SystemFlag.key == flag_key))
        if result.scalar_one_or_none():
            return  # already ran

        res = await session.execute(
            select(PaperPortfolio).where(
                PaperPortfolio.balance == 10000.0,
                PaperPortfolio.initial_balance == 10000.0,
            )
        )
        stale_portfolios = res.scalars().all()

        updated = 0
        for p in stale_portfolios:
            p.balance = 100.0
            p.initial_balance = 100.0
            updated += 1

        session.add(SystemFlag(key=flag_key, value=str(updated)))
        await session.commit()

    logger.info(f"✅ One-time paper balance reset complete: {updated} portfolio(s) moved from $10,000 → $100")


async def migrate_paper_trade_schema():
    """
    Adds columns to paper_trades that exist on the PaperTrade model but may be
    missing from the live table (create_all only creates new tables, it never
    alters existing ones).
    """
    statements = [
        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS highest_price DOUBLE PRECISION",
        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS lowest_price DOUBLE PRECISION",
        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS remaining_quantity DOUBLE PRECISION",
        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS realized_pnl DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS initial_entry_price DOUBLE PRECISION",
        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS dca_fills INTEGER DEFAULT 0",
        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS last_dca_price DOUBLE PRECISION",
        "ALTER TABLE paper_settings ADD COLUMN IF NOT EXISTS daily_trade_limit INTEGER DEFAULT 10",
    ]

    async with engine.begin() as conn:
        for s in statements:
            await conn.execute(text(s))

    logger.info("✅ Paper Trade Schema Migration Complete")


async def get_or_create_portfolio(user_id: int) -> PaperPortfolio:
    async with async_session() as session:
        result = await session.execute(
            select(PaperPortfolio).where(PaperPortfolio.user_id == user_id)
        )
        portfolio = result.scalar_one_or_none()

        if not portfolio:
            portfolio = PaperPortfolio(user_id=user_id)
            session.add(portfolio)
            await session.commit()
            await session.refresh(portfolio)

        return portfolio


async def get_or_create_settings(user_id: int) -> PaperSettings:
    async with async_session() as session:
        result = await session.execute(
            select(PaperSettings).where(PaperSettings.user_id == user_id)
        )
        settings = result.scalar_one_or_none()

        if not settings:
            settings = PaperSettings(user_id=user_id)
            session.add(settings)
            await session.commit()
            await session.refresh(settings)

        return settings


async def update_setting(user_id: int, field: str, value) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(PaperSettings).where(PaperSettings.user_id == user_id)
        )
        settings = result.scalar_one_or_none()

        if not settings:
            settings = PaperSettings(user_id=user_id)
            session.add(settings)

        setattr(settings, field, value)
        await session.commit()


async def get_autobuy_settings() -> list[PaperSettings]:
    async with async_session() as session:
        result = await session.execute(
            select(PaperSettings).where(PaperSettings.auto_buy == True)
        )
        return result.scalars().all()


# ============================================================
# User-Configurable Auto-Buy Filters
# ============================================================

async def get_or_create_filters(user_id: int) -> PaperAutoBuyFilter:
    async with async_session() as session:
        result = await session.execute(
            select(PaperAutoBuyFilter).where(PaperAutoBuyFilter.user_id == user_id)
        )
        filters = result.scalar_one_or_none()

        if not filters:
            filters = PaperAutoBuyFilter(user_id=user_id)
            session.add(filters)
            await session.commit()
            await session.refresh(filters)

        return filters


async def update_filter(user_id: int, field: str, value) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(PaperAutoBuyFilter).where(PaperAutoBuyFilter.user_id == user_id)
        )
        filters = result.scalar_one_or_none()

        if not filters:
            filters = PaperAutoBuyFilter(user_id=user_id)
            session.add(filters)

        setattr(filters, field, value)
        await session.commit()


async def get_filters_map(user_ids: list[int]) -> dict:
    """
    Batch-fetches auto-buy filter rows for a list of user IDs, keyed by
    user_id. Used by the auto-buy pipeline so it doesn't issue a separate
    query per subscriber on every signal. Users with no saved filter row
    simply won't appear in the returned dict (treated as "no filters set").
    """
    if not user_ids:
        return {}

    async with async_session() as session:
        result = await session.execute(
            select(PaperAutoBuyFilter).where(PaperAutoBuyFilter.user_id.in_(user_ids))
        )
        rows = result.scalars().all()

    return {row.user_id: row for row in rows}


async def get_trades_opened_today_count(user_id: int) -> int:
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count(PaperTrade.id)).where(
                PaperTrade.user_id == user_id,
                PaperTrade.opened_at >= today_start,
            )
        )
        return result.scalar() or 0


async def get_open_positions_count(user_id: int) -> int:
    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count(PaperTrade.id))
            .where(PaperTrade.user_id == user_id, PaperTrade.status == "open")
        )
        return result.scalar() or 0


async def open_paper_trade(
    user_id: int,
    contract: str,
    name: str,
    symbol: str,
    entry_price: float,
    usd_amount: float,
    take_profit_pct: float,
    stop_loss_pct: float,
) -> dict:
    portfolio = await get_or_create_portfolio(user_id)

    if portfolio.balance < usd_amount:
        return {"ok": False, "reason": "Insufficient virtual balance"}

    token_quantity = usd_amount / entry_price if entry_price > 0 else 0

    if token_quantity <= 0:
        return {"ok": False, "reason": "Invalid entry price"}

    open_count = await get_open_positions_count(user_id)
    if open_count >= 10:  # Hard limit for safety
        return {"ok": False, "reason": "Maximum open positions reached"}

    async with async_session() as session:
        result = await session.execute(
            select(PaperPortfolio).where(PaperPortfolio.user_id == user_id)
        )
        portfolio = result.scalar_one_or_none()
        portfolio.balance -= usd_amount

        trade = PaperTrade(
            user_id=user_id,
            contract=contract,
            name=name,
            symbol=symbol,
            entry_price=entry_price,
            current_price=entry_price,
            highest_price=entry_price,
            lowest_price=entry_price,
            usd_invested=usd_amount,
            token_quantity=token_quantity,
            remaining_quantity=token_quantity,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            status="open",
            initial_entry_price=entry_price,
            dca_fills=0,
        )

        session.add(trade)
        await session.commit()
        await session.refresh(trade)

    logger.info(f"Paper BUY executed for user {user_id}: {symbol} ${usd_amount} @ ${entry_price:.8f}")

    return {
        "ok": True,
        "trade_id": trade.id,
        "symbol": symbol,
        "name": name,
        "contract": contract,
        "entry_price": entry_price,
        "usd_invested": usd_amount,
        "token_quantity": token_quantity,
        "balance_remaining": portfolio.balance,
    }


async def get_open_trade_by_contract(user_id: int, contract: str) -> PaperTrade | None:
    async with async_session() as session:
        result = await session.execute(
            select(PaperTrade).where(
                PaperTrade.user_id == user_id,
                PaperTrade.contract == contract,
                PaperTrade.status == "open",
            )
        )
        return result.scalar_one_or_none()


async def execute_paper_buy(
    user_id: int,
    contract: str,
    name: str,
    symbol: str,
    current_price: float,
    is_auto: bool = False,
) -> dict:
    """
    Wraps open_paper_trade with the user's saved PaperSettings
    (buy amount, TP/SL defaults, and configurable max open positions).

    is_auto=True marks this as a signal-triggered auto-buy — only auto-buys
    are subject to the per-user daily_trade_limit; manual "Paper Buy" clicks
    are never limited by it.

    DCA integration: if the user already has an OPEN position in this exact
    contract and has DCA enabled, this merges into that position as a DCA
    fill (see add_dca_fill) instead of opening a second, separate trade —
    this is the "integrates seamlessly with Auto Buy" requirement. Without
    DCA enabled, behavior is unchanged from before (a second buy on an
    already-open contract still just opens a normal new position).
    """
    settings = await get_or_create_settings(user_id)

    existing = await get_open_trade_by_contract(user_id, contract)
    if existing:
        dca_settings = await get_or_create_dca_settings(user_id)
        if dca_settings.enabled:
            max_entries = _effective_max_entries(dca_settings)
            if (existing.dca_fills or 0) + 1 < max_entries:
                return await add_dca_fill(
                    trade_id=existing.id,
                    price=current_price,
                    usd_amount=settings.buy_amount_usd,
                    reason="duplicate_signal_merge",
                )
            return {"ok": False, "reason": "DCA max entries already reached for this position"}
        # DCA disabled: fall through to normal behavior (unchanged).

    open_count = await get_open_positions_count(user_id)
    if open_count >= settings.max_open_positions:
        return {
            "ok": False,
            "reason": f"Maximum open positions reached ({settings.max_open_positions})",
        }

    if is_auto:
        daily_limit = settings.daily_trade_limit or 10
        today_count = await get_trades_opened_today_count(user_id)
        if today_count >= daily_limit:
            return {
                "ok": False,
                "reason": f"Daily auto-buy limit reached ({daily_limit}/day)",
            }

    return await open_paper_trade(
        user_id=user_id,
        contract=contract,
        name=name,
        symbol=symbol,
        entry_price=current_price,
        usd_amount=settings.buy_amount_usd,
        take_profit_pct=settings.take_profit_pct,
        stop_loss_pct=settings.stop_loss_pct,
    )


async def get_trade_by_id(trade_id: int, user_id: int) -> PaperTrade | None:
    async with async_session() as session:
        result = await session.execute(
            select(PaperTrade).where(PaperTrade.id == trade_id, PaperTrade.user_id == user_id)
        )
        return result.scalar_one_or_none()


# ============================================================
# DCA (Dollar-Cost Averaging) Strategy
#
# Extends the existing Auto-Buy engine rather than replacing it: DCA
# fills go through the same PaperTrade/PaperPortfolio rows as a normal
# buy, just adding to an already-open position instead of opening a new
# one. Two trigger paths both funnel into add_dca_fill():
#   1. execute_paper_buy() above — a duplicate signal for a contract the
#      user already holds, merged as a fill instead of a new position.
#   2. check_and_apply_dca() below — a price-drawdown ladder level being
#      crossed, called from services/paper_monitor.py's existing price
#      loop using the price it already fetched (no extra API calls).
# ============================================================

async def get_or_create_dca_settings(user_id: int) -> PaperDCASettings:
    async with async_session() as session:
        result = await session.execute(
            select(PaperDCASettings).where(PaperDCASettings.user_id == user_id)
        )
        settings = result.scalar_one_or_none()

        if not settings:
            settings = PaperDCASettings(user_id=user_id)
            session.add(settings)
            await session.commit()
            await session.refresh(settings)

        return settings


async def update_dca_setting(user_id: int, field: str, value) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(PaperDCASettings).where(PaperDCASettings.user_id == user_id)
        )
        settings = result.scalar_one_or_none()

        if not settings:
            settings = PaperDCASettings(user_id=user_id)
            session.add(settings)

        setattr(settings, field, value)
        await session.commit()


def get_dca_custom_levels(settings: PaperDCASettings) -> list[dict] | None:
    """Parses custom_levels_json into a list of {"drop_pct", "amount_usd"}
    sorted ascending by drop_pct. Returns None if no custom ladder is set
    (caller should fall back to the flat default trigger/amount)."""
    if not settings.custom_levels_json:
        return None
    try:
        levels = json.loads(settings.custom_levels_json)
        if not isinstance(levels, list) or not levels:
            return None
        cleaned = [
            {"drop_pct": float(lv["drop_pct"]), "amount_usd": float(lv["amount_usd"])}
            for lv in levels
            if "drop_pct" in lv and "amount_usd" in lv
        ]
        return sorted(cleaned, key=lambda lv: lv["drop_pct"]) or None
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def parse_custom_dca_levels(text: str) -> tuple[list[dict] | None, str | None]:
    """
    Parses user-typed custom DCA ladder input, e.g. "15:20, 30:20, 50:30"
    (drop%%:usd_amount pairs) into the level list stored on
    PaperDCASettings.custom_levels_json. Returns (levels, error_message).
    """
    raw = (text or "").strip()
    if not raw:
        return None, "Please enter at least one level, e.g. 15:20, 30:20"

    levels = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            return None, f"'{chunk}' isn't in drop%:amount format, e.g. 15:20"
        drop_str, amt_str = chunk.split(":", 1)
        try:
            drop_pct = float(drop_str.strip().replace("%", ""))
            amount_usd = float(amt_str.strip().replace("$", ""))
        except (ValueError, TypeError):
            return None, f"'{chunk}' isn't a valid drop%:amount pair"

        if drop_pct <= 0 or drop_pct >= 100:
            return None, "Drop percentage must be between 0 and 100."
        if amount_usd <= 0:
            return None, "Amount must be greater than zero."

        levels.append({"drop_pct": drop_pct, "amount_usd": amount_usd})

    if not levels:
        return None, "Please enter at least one level, e.g. 15:20, 30:20"
    if len(levels) > 5:
        return None, "Maximum 5 custom DCA levels."

    return sorted(levels, key=lambda lv: lv["drop_pct"]), None


async def set_dca_custom_levels(user_id: int, levels: list[dict] | None) -> None:
    value = json.dumps(levels) if levels else None
    await update_dca_setting(user_id, "custom_levels_json", value)


def _effective_max_entries(settings: PaperDCASettings) -> int:
    """Total buys (initial + DCA fills) allowed for one position."""
    custom = get_dca_custom_levels(settings)
    if custom:
        return len(custom) + 1
    return max(1, settings.max_entries or 3)


def _next_dca_level(settings: PaperDCASettings, fills_so_far: int) -> dict | None:
    """
    Returns {"drop_pct", "amount_usd"} for the NEXT DCA fill (fills_so_far
    is trade.dca_fills, i.e. 0 before any DCA add-in has happened), or
    None if there's no next level (max entries reached).
    """
    custom = get_dca_custom_levels(settings)
    if custom:
        if fills_so_far >= len(custom):
            return None
        return custom[fills_so_far]

    max_entries = max(1, settings.max_entries or 3)
    if fills_so_far + 1 >= max_entries:
        return None
    return {
        "drop_pct": settings.default_trigger_drop_pct or 15.0,
        "amount_usd": settings.default_entry_amount_usd or 25.0,
    }


async def add_dca_fill(trade_id: int, price: float, usd_amount: float, reason: str = "price_drawdown") -> dict:
    """
    Core DCA fill: adds usd_amount worth of tokens at `price` to an
    already-open position, recomputing the weighted-average entry_price,
    total usd_invested, and quantity — the same fields every other part
    of the app (positions view, TP/SL check, close/partial-close PnL)
    already reads, so DCA fills flow through automatically with no
    changes needed anywhere else.
    """
    if price <= 0 or usd_amount <= 0:
        return {"ok": False, "reason": "Invalid price or amount"}

    async with async_session() as session:
        result = await session.execute(select(PaperTrade).where(PaperTrade.id == trade_id))
        trade = result.scalar_one_or_none()

        if not trade or trade.status != "open":
            return {"ok": False, "reason": "Position not found or already closed"}

        port_result = await session.execute(
            select(PaperPortfolio).where(PaperPortfolio.user_id == trade.user_id)
        )
        portfolio = port_result.scalar_one_or_none()

        if not portfolio or portfolio.balance < usd_amount:
            return {"ok": False, "reason": "Insufficient virtual balance for DCA fill"}

        new_tokens = usd_amount / price
        new_total_invested = trade.usd_invested + usd_amount
        new_total_quantity = trade.token_quantity + new_tokens
        new_remaining = (trade.remaining_quantity if trade.remaining_quantity is not None else trade.token_quantity) + new_tokens
        new_avg_price = new_total_invested / new_total_quantity if new_total_quantity > 0 else trade.entry_price

        portfolio.balance -= usd_amount

        trade.usd_invested = new_total_invested
        trade.token_quantity = new_total_quantity
        trade.remaining_quantity = new_remaining
        trade.entry_price = new_avg_price
        trade.current_price = price
        trade.dca_fills = (trade.dca_fills or 0) + 1
        trade.last_dca_price = price

        session.add(PaperDcaFill(
            user_id=trade.user_id,
            trade_id=trade.id,
            contract=trade.contract,
            symbol=trade.symbol,
            fill_number=trade.dca_fills + 1,  # +1 since the initial buy is fill #1
            trigger_reason=reason,
            price=price,
            usd_amount=usd_amount,
            token_quantity=new_tokens,
            new_avg_entry_price=new_avg_price,
            new_total_invested=new_total_invested,
        ))

        await session.commit()
        await session.refresh(trade)

    logger.info(
        f"DCA fill: user={trade.user_id} {trade.symbol} +${usd_amount:.2f} @ ${price:.8f} "
        f"(fill #{trade.dca_fills + 1}, new avg entry ${new_avg_price:.8f}, reason={reason})"
    )

    return {
        "ok": True,
        "dca_fill": True,
        "trade_id": trade.id,
        "user_id": trade.user_id,
        "name": trade.name,
        "symbol": trade.symbol,
        "contract": trade.contract,
        "fill_number": trade.dca_fills + 1,
        "fill_price": price,
        "fill_usd_amount": usd_amount,
        "fill_token_quantity": new_tokens,
        "new_avg_entry_price": new_avg_price,
        "new_total_invested": new_total_invested,
        "balance_remaining": portfolio.balance,
        # Backward-compatible aliases: existing callers (auto-buy and
        # manual "Paper Buy" notifications) read these exact keys from a
        # normal open_paper_trade() result. Filled in here too so a DCA
        # merge renders correctly wherever it's mistaken for a fresh
        # trade-open by code that hasn't been updated to check
        # result.get("dca_fill") explicitly.
        "entry_price": new_avg_price,
        "usd_invested": new_total_invested,
        "token_quantity": new_total_quantity,
    }


async def check_and_apply_dca(trade: PaperTrade, current_price: float) -> dict | None:
    """
    Called from paper_monitor's price loop (reusing the price it already
    fetched — no extra API call). If DCA is enabled for this trade's user
    and price has dropped through the next configured level below the
    ORIGINAL entry price, executes that fill and returns its result dict.
    Returns None if no fill happened (DCA disabled, no level reached yet,
    or max entries already used).

    Levels are anchored to initial_entry_price (the very first fill),
    not the live weighted-average entry_price — using the average would
    make each level's trigger point silently drift deeper after every
    fill (since averaging in a lower price pulls the average down too),
    which would no longer match the percentages the user configured.
    """
    anchor_price = trade.initial_entry_price or trade.entry_price
    if current_price <= 0 or not anchor_price or anchor_price <= 0:
        return None

    settings = await get_or_create_dca_settings(trade.user_id)
    if not settings.enabled:
        return None

    next_level = _next_dca_level(settings, trade.dca_fills or 0)
    if not next_level:
        return None

    drop_pct = ((anchor_price - current_price) / anchor_price) * 100
    if drop_pct < next_level["drop_pct"]:
        return None

    return await add_dca_fill(
        trade_id=trade.id,
        price=current_price,
        usd_amount=next_level["amount_usd"],
        reason="price_drawdown",
    )


async def get_dca_fills(trade_id: int, limit: int = 20) -> list[PaperDcaFill]:
    async with async_session() as session:
        result = await session.execute(
            select(PaperDcaFill)
            .where(PaperDcaFill.trade_id == trade_id)
            .order_by(PaperDcaFill.occurred_at.desc())
            .limit(limit)
        )
        return result.scalars().all()


async def partial_close_paper_trade(trade_id: int, exit_price: float, sell_pct: float, reason: str = "moonbag_sell") -> dict:
    """
    Sells a percentage of the remaining position (moonbag). If the sell
    percentage consumes the entire remaining quantity, the trade is fully
    closed and portfolio win/loss stats are finalized at that point.
    """
    async with async_session() as session:
        result = await session.execute(select(PaperTrade).where(PaperTrade.id == trade_id))
        trade = result.scalar_one_or_none()

        if not trade or trade.status != "open":
            return {"ok": False, "reason": "Trade not found or already closed"}

        sell_pct = max(0.0, min(sell_pct, 100.0))
        remaining = trade.remaining_quantity if trade.remaining_quantity is not None else trade.token_quantity
        sell_qty = remaining * (sell_pct / 100.0)

        if sell_qty <= 0:
            return {"ok": False, "reason": "Nothing left to sell"}

        proceeds = sell_qty * exit_price
        cost_basis_portion = (trade.usd_invested * (sell_qty / trade.token_quantity)) if trade.token_quantity > 0 else 0.0
        pnl_usd = proceeds - cost_basis_portion
        pnl_pct = (pnl_usd / cost_basis_portion) * 100 if cost_basis_portion > 0 else 0.0

        new_remaining = remaining - sell_qty
        trade.remaining_quantity = new_remaining
        trade.realized_pnl = (trade.realized_pnl or 0.0) + pnl_usd
        trade.current_price = exit_price

        port_result = await session.execute(
            select(PaperPortfolio).where(PaperPortfolio.user_id == trade.user_id)
        )
        portfolio = port_result.scalar_one_or_none()

        if portfolio:
            portfolio.balance += proceeds
            portfolio.net_pnl += pnl_usd

        fully_closed = new_remaining <= 1e-9

        if fully_closed:
            trade.status = reason
            trade.exit_reason = reason
            trade.exit_price = exit_price
            trade.pnl_usd = trade.realized_pnl
            trade.pnl_pct = (trade.realized_pnl / trade.usd_invested) * 100 if trade.usd_invested > 0 else 0.0
            trade.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)

            if portfolio:
                portfolio.total_trades += 1
                if trade.pnl_usd >= 0:
                    portfolio.winning_trades += 1
                    portfolio.total_profit += trade.pnl_usd
                    if trade.pnl_usd > portfolio.best_trade_pnl:
                        portfolio.best_trade_pnl = trade.pnl_usd
                else:
                    portfolio.losing_trades += 1
                    portfolio.total_loss += abs(trade.pnl_usd)
                    if trade.pnl_usd < portfolio.worst_trade_pnl:
                        portfolio.worst_trade_pnl = trade.pnl_usd

        session.add(PaperPnlEvent(
            user_id=trade.user_id,
            trade_id=trade.id,
            symbol=trade.symbol,
            pnl_usd=pnl_usd,
        ))

        await session.commit()

    logger.info(f"Paper moonbag sell: {trade.symbol} sold {sell_pct:.0f}% PnL: ${pnl_usd:.2f} fully_closed={fully_closed}")

    return {
        "ok": True,
        "user_id": trade.user_id,
        "symbol": trade.symbol,
        "name": trade.name,
        "contract": trade.contract,
        "sell_pct": sell_pct,
        "sell_qty": sell_qty,
        "exit_price": exit_price,
        "proceeds": proceeds,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "remaining_quantity": new_remaining,
        "fully_closed": fully_closed,
    }


async def close_paper_trade(trade_id: int, exit_price: float, reason: str) -> dict:
    async with async_session() as session:
        result = await session.execute(
            select(PaperTrade).where(PaperTrade.id == trade_id)
        )
        trade = result.scalar_one_or_none()

        if not trade or trade.status != "open":
            return {"ok": False, "reason": "Trade not found or already closed"}

        current_value = trade.token_quantity * exit_price
        pnl_usd = current_value - trade.usd_invested
        pnl_pct = (pnl_usd / trade.usd_invested) * 100 if trade.usd_invested > 0 else 0

        trade.exit_price = exit_price
        trade.current_price = exit_price
        trade.pnl_usd = pnl_usd
        trade.pnl_pct = pnl_pct
        trade.status = reason
        trade.exit_reason = reason
        trade.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)

        port_result = await session.execute(
            select(PaperPortfolio).where(PaperPortfolio.user_id == trade.user_id)
        )
        portfolio = port_result.scalar_one_or_none()

        if portfolio:
            portfolio.balance += current_value
            portfolio.total_trades += 1
            portfolio.net_pnl += pnl_usd

            if pnl_usd >= 0:
                portfolio.winning_trades += 1
                portfolio.total_profit += pnl_usd
                if pnl_usd > portfolio.best_trade_pnl:
                    portfolio.best_trade_pnl = pnl_usd
            else:
                portfolio.losing_trades += 1
                portfolio.total_loss += abs(pnl_usd)
                if pnl_usd < portfolio.worst_trade_pnl:
                    portfolio.worst_trade_pnl = pnl_usd

        session.add(PaperPnlEvent(
            user_id=trade.user_id,
            trade_id=trade.id,
            symbol=trade.symbol,
            pnl_usd=pnl_usd,
        ))

        await session.commit()

    logger.info(f"Paper trade closed: {trade.symbol} PnL: ${pnl_usd:.2f} ({pnl_pct:.1f}%) Reason: {reason}")

    return {
        "ok": True,
        "user_id": trade.user_id,
        "symbol": trade.symbol,
        "name": trade.name,
        "contract": trade.contract,
        "entry_price": trade.entry_price,
        "exit_price": exit_price,
        "usd_invested": trade.usd_invested,
        "current_value": current_value,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "reason": reason,
        "holding_time": str(trade.closed_at - trade.opened_at) if trade.closed_at and trade.opened_at else "N/A",
    }


async def get_open_trades(user_id: int) -> list[PaperTrade]:
    async with async_session() as session:
        result = await session.execute(
            select(PaperTrade)
            .where(PaperTrade.user_id == user_id, PaperTrade.status == "open")
            .order_by(PaperTrade.opened_at.desc())
        )
        return result.scalars().all()


async def get_all_trades(user_id: int, limit: int = 20) -> list[PaperTrade]:
    async with async_session() as session:
        result = await session.execute(
            select(PaperTrade)
            .where(PaperTrade.user_id == user_id)
            .order_by(PaperTrade.opened_at.desc())
            .limit(limit)
        )
        return result.scalars().all()


async def get_all_open_trades() -> list[PaperTrade]:
    async with async_session() as session:
        result = await session.execute(
            select(PaperTrade).where(PaperTrade.status == "open")
        )
        return result.scalars().all()


async def reset_portfolio(user_id: int) -> dict:
    """
    Manually resets a user's paper portfolio: closes any open positions
    (marked as 'portfolio_reset', no PnL awarded since they're wiped, not
    sold), clears all stats, and restores the balance to the CURRENT
    default (100.0) regardless of what it was before. This is the
    user-triggered version of the one-time migration — useful when a
    portfolio is still stuck on stale data for any reason.
    """
    async with async_session() as session:
        result = await session.execute(
            select(PaperTrade).where(PaperTrade.user_id == user_id, PaperTrade.status == "open")
        )
        open_trades = result.scalars().all()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for t in open_trades:
            t.status = "portfolio_reset"
            t.exit_reason = "portfolio_reset"
            t.remaining_quantity = 0.0
            t.closed_at = now

        port_result = await session.execute(
            select(PaperPortfolio).where(PaperPortfolio.user_id == user_id)
        )
        portfolio = port_result.scalar_one_or_none()

        default_balance = PaperPortfolio.__table__.c.balance.default.arg

        if portfolio:
            portfolio.balance = default_balance
            portfolio.initial_balance = default_balance
            portfolio.total_trades = 0
            portfolio.winning_trades = 0
            portfolio.losing_trades = 0
            portfolio.total_profit = 0.0
            portfolio.total_loss = 0.0
            portfolio.net_pnl = 0.0
            portfolio.best_trade_pnl = 0.0
            portfolio.worst_trade_pnl = 0.0
        else:
            portfolio = PaperPortfolio(user_id=user_id, balance=default_balance, initial_balance=default_balance)
            session.add(portfolio)

        await session.commit()

    logger.info(f"Paper portfolio manually reset for user {user_id} -> ${default_balance}")

    return {"ok": True, "new_balance": default_balance, "closed_positions": len(open_trades)}


async def archive_daily_trades_for_all_users(for_date: "datetime" = None) -> int:
    """
    Closes out and archives every user's daily paper-trading history for
    `for_date` (defaults to "today", UTC) into DailyTradeArchive.

    IMPORTANT — this function is purely additive/historical:
      - It NEVER deletes or mutates PaperTrade rows (open positions carry
        over across the day boundary exactly as they are).
      - It NEVER touches PaperPortfolio.balance (that's live account
        equity, not daily history).
      - It NEVER touches PaperSettings, PaperAutoBuyFilter, Watchlist, or
        TrackedWallet — user settings, preferences, watchlists, and wallets
        are completely untouched by the daily reset.

    A "fresh trading day" for auto-buy limits already starts naturally at
    midnight because get_trades_opened_today_count() filters on today's
    date live — so there is nothing to reset there either; this function's
    only job is writing the historical record of the day that just ended.
    Safe to call multiple times for the same day (upserts by user+date).
    """
    from models.daily_trade_archive import DailyTradeArchive

    target = (for_date or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day_start = target.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    day_end = target.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=None)
    date_str = target.strftime("%Y-%m-%d")

    async with async_session() as session:
        user_ids_res = await session.execute(
            select(PaperTrade.user_id)
            .where(PaperTrade.opened_at >= day_start, PaperTrade.opened_at <= day_end)
            .union(
                select(PaperPnlEvent.user_id)
                .where(PaperPnlEvent.occurred_at >= day_start, PaperPnlEvent.occurred_at <= day_end)
            )
        )
        user_ids = {row[0] for row in user_ids_res.all()}

    archived = 0
    for user_id in user_ids:
        async with async_session() as session:
            opened_res = await session.execute(
                select(sa_func.count(PaperTrade.id)).where(
                    PaperTrade.user_id == user_id,
                    PaperTrade.opened_at >= day_start,
                    PaperTrade.opened_at <= day_end,
                )
            )
            trades_opened = opened_res.scalar() or 0

            closed_res = await session.execute(
                select(sa_func.count(PaperTrade.id)).where(
                    PaperTrade.user_id == user_id,
                    PaperTrade.closed_at.isnot(None),
                    PaperTrade.closed_at >= day_start,
                    PaperTrade.closed_at <= day_end,
                )
            )
            trades_closed = closed_res.scalar() or 0

            pnl_rows_res = await session.execute(
                select(PaperPnlEvent.pnl_usd).where(
                    PaperPnlEvent.user_id == user_id,
                    PaperPnlEvent.occurred_at >= day_start,
                    PaperPnlEvent.occurred_at <= day_end,
                )
            )
            pnl_values = [row[0] for row in pnl_rows_res.all()]
            wins = sum(1 for v in pnl_values if v >= 0)
            losses = sum(1 for v in pnl_values if v < 0)
            net_pnl_usd = sum(pnl_values)

            portfolio_res = await session.execute(
                select(PaperPortfolio.balance).where(PaperPortfolio.user_id == user_id)
            )
            ending_balance = portfolio_res.scalar()

            existing_res = await session.execute(
                select(DailyTradeArchive).where(
                    DailyTradeArchive.user_id == user_id,
                    DailyTradeArchive.archive_date == date_str,
                )
            )
            existing = existing_res.scalar_one_or_none()

            if existing:
                existing.trades_opened = trades_opened
                existing.trades_closed = trades_closed
                existing.wins = wins
                existing.losses = losses
                existing.net_pnl_usd = net_pnl_usd
                existing.ending_balance = ending_balance
            else:
                session.add(DailyTradeArchive(
                    user_id=user_id,
                    archive_date=date_str,
                    trades_opened=trades_opened,
                    trades_closed=trades_closed,
                    wins=wins,
                    losses=losses,
                    net_pnl_usd=net_pnl_usd,
                    ending_balance=ending_balance,
                ))

            await session.commit()
            archived += 1

    logger.info(f"✅ Daily trade archive complete for {date_str}: {archived} user(s) archived")
    return archived


async def daily_trade_reset_loop(bot=None):
    """
    Scheduler for feature 6 (Daily Trade Reset):
      - At 11:59 PM (UTC), archives + closes out each user's day via
        archive_daily_trades_for_all_users().
      - At 12:00 AM, a fresh trading day begins automatically (no data
        mutation needed — see archive_daily_trades_for_all_users docstring
        for why nothing needs to be reset there).
    Runs forever, recomputing the next 11:59 PM boundary each cycle so it
    never drifts.
    """
    logger.info("🕧 Daily trade archive/reset scheduler active (11:59 PM UTC)")

    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=23, minute=59, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        sleep_seconds = max((target - now).total_seconds(), 1)

        await asyncio.sleep(sleep_seconds)

        try:
            await archive_daily_trades_for_all_users()
        except Exception as e:
            logger.error(f"Daily trade archive failed (non-fatal): {e}")

        # Cross midnight into the fresh trading day; nothing to reset, this
        # sleep just prevents re-triggering the archive twice in one minute.
        await asyncio.sleep(90)


async def get_pnl_calendar(user_id: int, year: int, month: int) -> dict:
    """
    Returns a day -> net realized PnL (USD) map for the given month, built
    from the PaperPnlEvent ledger (covers both full closes and partial
    moonbag sells). Also returns the month's total PnL and trade-day count.
    """
    start = datetime(year, month, 1)
    days_in_month = calendar.monthrange(year, month)[1]
    end = datetime(year, month, days_in_month, 23, 59, 59)

    async with async_session() as session:
        result = await session.execute(
            select(PaperPnlEvent.occurred_at, PaperPnlEvent.pnl_usd)
            .where(
                PaperPnlEvent.user_id == user_id,
                PaperPnlEvent.occurred_at >= start,
                PaperPnlEvent.occurred_at <= end,
            )
        )
        rows = result.all()

    daily = {}
    for occurred_at, pnl_usd in rows:
        day = occurred_at.day
        daily[day] = daily.get(day, 0.0) + pnl_usd

    return {
        "year": year,
        "month": month,
        "days_in_month": days_in_month,
        "daily_pnl": daily,
        "total_pnl": sum(daily.values()),
        "trading_days": len(daily),
        "green_days": sum(1 for v in daily.values() if v >= 0),
        "red_days": sum(1 for v in daily.values() if v < 0),
    }
