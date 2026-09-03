import asyncio
import html
import logging

from sqlalchemy import select

from config.settings import HELIUS_WALLET_CACHE_TTL_SECONDS
from providers.marketdata.dexscreener import get_token_card_info
from providers.rpc.helius_request_manager import PRIORITY_HIGH
from domain.intelligence.solana_token_holdings import fetch_wallet_holdings
from infra.db.session import async_session
from models.real_trade import RealTrade
from models.real_wallet import RealWallet

logger = logging.getLogger("AlphaPulse.WalletPortfolio")

WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
MAX_PORTFOLIO_TOKENS = 50


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else "N/A"


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def format_usd(value) -> str:
    try:
        num = float(value)
        if num >= 1_000_000_000:
            return f"${num / 1_000_000_000:.2f}B"
        if num >= 1_000_000:
            return f"${num / 1_000_000:.2f}M"
        if num >= 1_000:
            return f"${num / 1_000:.2f}K"
        return f"${num:,.2f}"
    except (ValueError, TypeError):
        return "N/A"


def format_price(value) -> str:
    try:
        num = float(value)
        if num >= 1:
            return f"${num:,.4f}"
        if num >= 0.000001:
            return f"${num:.8f}".rstrip("0").rstrip(".")
        if num > 0:
            return f"${num:.12f}".rstrip("0").rstrip(".")
        return "N/A"
    except (ValueError, TypeError):
        return "N/A"


def format_token_amount(value) -> str:
    try:
        num = float(value)
        if num >= 1_000_000_000:
            return f"{num / 1_000_000_000:.2f}B"
        if num >= 1_000_000:
            return f"{num / 1_000_000:.2f}M"
        if num >= 1_000:
            return f"{num / 1_000:.2f}K"
        return f"{num:,.4f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return "N/A"


async def fetch_wallet_fungible_tokens(wallet_address: str, limit: int = MAX_PORTFOLIO_TOKENS) -> list[dict] | None:
    try:
        holdings = await fetch_wallet_holdings(
            wallet_address,
            priority=PRIORITY_HIGH,
            cache_key_prefix="wallet_portfolio",
            cache_ttl=HELIUS_WALLET_CACHE_TTL_SECONDS,
        )
    except Exception as e:
        logger.error(f"Wallet portfolio fetch error for {wallet_address}: {e}")
        return None

    if holdings is None:
        logger.warning(f"Wallet portfolio unavailable for {wallet_address}")
        return None

    tokens = []
    if holdings["native_sol"] > 0:
        tokens.append({"mint": WRAPPED_SOL_MINT, "name": "Solana", "symbol": "SOL", "amount": holdings["native_sol"]})

    max_tokens = max(1, min(limit, MAX_PORTFOLIO_TOKENS))
    for token in holdings["tokens"]:
        if len(tokens) >= max_tokens:
            break
        tokens.append({"mint": token["mint"], "name": "Unknown", "symbol": "???", "amount": token["amount"]})

    logger.info(f"Wallet {wallet_address} tokens found: {len(tokens)}")
    return tokens


async def enrich_single_token(token: dict) -> dict:
    mint = token["mint"]
    amount = token["amount"]
    price = 0.0
    value = 0.0
    pair_url = ""
    priced = False

    try:
        token_data = await asyncio.wait_for(get_token_card_info(mint), timeout=6)
        if token_data:
            price = _to_float(token_data.get("price"))
            value = amount * price
            pair_url = token_data.get("pair_url", "")
            priced = price > 0
            token["name"] = token_data.get("name") or token["name"]
            token["symbol"] = token_data.get("symbol") or token["symbol"]
    except asyncio.TimeoutError:
        logger.warning(f"DexScreener timeout pricing token {mint}")
    except Exception as e:
        logger.warning(f"Could not price token {mint}: {e}")

    # SOL is represented internally by the wrapped SOL mint for SPL/RPC
    # portfolio accounting, but the user-facing portfolio must call it SOL,
    # never "Wrapped SOL". Keep this normalization after market-data
    # enrichment so provider metadata cannot overwrite the display name.
    if mint == WRAPPED_SOL_MINT:
        token["name"] = "SOL"
        token["symbol"] = "SOL"

    return {**token, "price": price, "value": value, "pair_url": pair_url, "priced": priced}


async def enrich_wallet_tokens(tokens: list[dict]) -> list[dict]:
    if not tokens:
        return []
    enriched = await asyncio.gather(*[enrich_single_token(token) for token in tokens], return_exceptions=False)
    enriched.sort(key=lambda x: x["value"], reverse=True)
    return enriched


async def _get_open_trade_cost_basis(user_id: int) -> dict[str, dict]:
    async with async_session() as session:
        result = await session.execute(
            select(RealTrade).where(
                RealTrade.user_id == user_id,
                RealTrade.status.in_(["open", "selling"]),
            )
        )
        trades = result.scalars().all()

    basis: dict[str, dict] = {}
    for trade in trades:
        remaining = _to_float(trade.remaining_quantity)
        entry = _to_float(trade.entry_price)
        if remaining <= 0 or entry <= 0:
            continue
        row = basis.setdefault(trade.contract, {"cost_basis_usd": 0.0, "quantity": 0.0})
        row["cost_basis_usd"] += remaining * entry
        row["quantity"] += remaining
    return basis


async def _resolve_user_id(wallet_address: str, user_id: int | None) -> int | None:
    if user_id is not None:
        return user_id
    try:
        async with async_session() as session:
            result = await session.execute(
                select(RealWallet.user_id).where(
                    RealWallet.public_key == wallet_address,
                    RealWallet.is_active.is_(True),
                )
            )
            return result.scalar_one_or_none()
    except Exception as e:
        logger.warning(f"Could not resolve RealWallet owner for portfolio: {e}")
        return None


async def _attach_pnl(tokens: list[dict], user_id: int | None) -> list[dict]:
    if user_id is None:
        for token in tokens:
            token.update({"cost_basis_usd": None, "pnl_usd": None, "pnl_pct": None, "pnl_known": False})
        return tokens

    try:
        basis = await _get_open_trade_cost_basis(user_id)
    except Exception as e:
        logger.warning(f"RealTrade cost-basis lookup failed for user {user_id}: {e}")
        basis = {}

    for token in tokens:
        row = basis.get(token["mint"])
        if not row or not token.get("priced"):
            token.update({"cost_basis_usd": None, "pnl_usd": None, "pnl_pct": None, "pnl_known": False})
            continue
        cost = row["cost_basis_usd"]
        pnl = token["value"] - cost
        pct = (pnl / cost * 100.0) if cost > 0 else 0.0
        token.update({"cost_basis_usd": cost, "pnl_usd": pnl, "pnl_pct": pct, "pnl_known": True})
    return tokens


async def get_wallet_portfolio_value(wallet_address: str, limit: int = MAX_PORTFOLIO_TOKENS) -> dict | None:
    try:
        tokens = await fetch_wallet_fungible_tokens(wallet_address, limit=limit)
        if tokens is None:
            return None
        if not tokens:
            return {"total_value_usd": 0.0, "token_count": 0, "priced_count": 0}
        enriched = await enrich_wallet_tokens(tokens)
        return {
            "total_value_usd": sum(token["value"] for token in enriched),
            "token_count": len(enriched),
            "priced_count": sum(1 for token in enriched if token["priced"]),
        }
    except Exception as e:
        logger.warning(f"Wallet portfolio value fetch error for {wallet_address}: {e}")
        return None


def _pnl_line(token: dict) -> str:
    if not token.get("pnl_known"):
        return "   📊 PnL: <b>N/A</b> <i>(no recorded AlphaPulse cost basis)</i>\n"
    pnl = token["pnl_usd"]
    pct = token["pnl_pct"]
    sign = "+" if pnl >= 0 else ""
    return f"   📊 PnL: <b>{sign}{format_usd(pnl)} ({sign}{pct:.1f}%)</b>\n"


async def build_wallet_portfolio_report(wallet_address: str, limit: int = MAX_PORTFOLIO_TOKENS, user_id: int | None = None) -> str:
    try:
        limit = max(1, min(limit, MAX_PORTFOLIO_TOKENS))
        tokens = await fetch_wallet_fungible_tokens(wallet_address, limit=limit)
        if tokens is None:
            return "⚠️ <b>Couldn't Load Wallet Portfolio</b>\n\nBlockchain data is temporarily unavailable. Your balance is <b>unknown</b>, not zero. Please try again shortly."
        if not tokens:
            return "📭 <b>No Wallet Holdings Found</b>\n\nThis wallet currently has no positive SOL/SPL holdings."

        enriched = await enrich_wallet_tokens(tokens)
        resolved_user_id = await _resolve_user_id(wallet_address, user_id)
        enriched = await _attach_pnl(enriched, resolved_user_id)

        total_value = sum(token["value"] for token in enriched if token["priced"])
        priced_count = sum(1 for token in enriched if token["priced"])
        pnl_known = [token for token in enriched if token.get("pnl_known")]
        total_cost = sum(token["cost_basis_usd"] for token in pnl_known)
        total_pnl = sum(token["pnl_usd"] for token in pnl_known)
        total_pnl_pct = (total_pnl / total_cost * 100.0) if total_cost > 0 else None

        text = (
            "💼 <b>Real Wallet Portfolio</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"💎 Total Value: <b>{format_usd(total_value)} USDT</b>\n"
            f"🪙 Holdings: <b>{len(enriched)}</b>\n"
            f"💵 Priced: <b>{priced_count}/{len(enriched)}</b>\n"
        )
        if total_cost > 0:
            sign = "+" if total_pnl >= 0 else ""
            text += f"📈 Recorded PnL: <b>{sign}{format_usd(total_pnl)} ({sign}{total_pnl_pct:.1f}%)</b>\n"
        else:
            text += "📈 Recorded PnL: <b>N/A</b>\n"
        text += "━━━━━━━━━━━━━━━━━━━━━\n\n"

        for index, token in enumerate(enriched, 1):
            name = _esc(token.get("name") or "Unknown")
            symbol = _esc(token.get("symbol") or "???")
            price = format_price(token["price"]) if token["priced"] else "N/A"
            value = format_usd(token["value"]) if token["priced"] else "N/A"
            text += (
                f"<b>{index}. {name} ({symbol})</b>\n"
                f"   🪙 Amount: <b>{format_token_amount(token['amount'])}</b>\n"
                f"   💵 Price: <b>{price}</b>\n"
                f"   💰 Value: <b>{value} USDT</b>\n"
                f"{_pnl_line(token)}\n"
            )

        return text + "━━━━━━━━━━━━━━━━━━━━━\n⚡ Live snapshot • AlphaPulse"
    except Exception as e:
        logger.error(f"Wallet portfolio report error: {e}")
        return "⚠️ <b>Wallet Portfolio Error</b>\n\nCould not build the holdings snapshot right now. Please try again shortly."
