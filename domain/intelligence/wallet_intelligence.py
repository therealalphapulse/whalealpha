import asyncio
import html
import logging

from config.settings import HELIUS_WALLET_CACHE_TTL_SECONDS
from providers.marketdata.dexscreener import get_token_card_info
from providers.marketdata.coingecko import get_solana_price
from providers.rpc.helius import get_recent_signatures
from providers.rpc.helius_request_manager import PRIORITY_LOW
from domain.intelligence.solana_token_holdings import fetch_wallet_holdings

logger = logging.getLogger("AlphaPulse.WalletIntelligence")

WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"


def _esc(value) -> str:
    if value is None:
        return "Unknown"

    text = str(value).strip()

    if not text or text.lower() in {"none", "null", "n/a"}:
        return "Unknown"

    return html.escape(text)


def _short(value: str, size: int = 8) -> str:
    if not value:
        return "Unknown"

    if len(value) <= size * 2:
        return value

    return f"{value[:size]}...{value[-size:]}"


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
        elif num >= 1_000_000:
            return f"${num / 1_000_000:.2f}M"
        elif num >= 1_000:
            return f"${num / 1_000:.2f}K"
        else:
            return f"${num:,.2f}"
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
        elif num >= 1:
            return f"{num:,.4f}".rstrip("0").rstrip(".")
        elif num > 0:
            return f"{num:.8f}".rstrip("0").rstrip(".")
        else:
            return "0"
    except (ValueError, TypeError):
        return "N/A"


def format_price(value) -> str:
    try:
        num = float(value)

        if num >= 1:
            return f"${num:,.4f}"
        elif num >= 0.000001:
            return f"${num:.8f}".rstrip("0").rstrip(".")
        elif num > 0:
            return f"${num:.12f}".rstrip("0").rstrip(".")
        else:
            return "N/A"
    except (ValueError, TypeError):
        return "N/A"


async def get_sol_price_usd() -> float:
    """
    Fetch SOL price from CoinGecko.
    Fallback: DexScreener WSOL.
    """

    try:
        sol = await get_solana_price()
        if sol and sol.get("price"):
            price = _to_float(sol.get("price"))
            if price > 0:
                return price
    except Exception as e:
        logger.warning(f"CoinGecko SOL price failed: {e}")

    try:
        wsol = await get_token_card_info(WRAPPED_SOL_MINT)
        if wsol and wsol.get("price"):
            price = _to_float(wsol.get("price"))
            if price > 0:
                return price
    except Exception as e:
        logger.warning(f"DexScreener WSOL price failed: {e}")

    return 0.0


def extract_token_name_symbol(item: dict, token_info: dict, mint: str) -> tuple[str, str]:
    """
    Robustly extract token name and symbol from Helius DAS response.
    Falls back to mint shortcode if metadata is missing.
    """

    content = item.get("content") or {}
    metadata = content.get("metadata") or {}

    name = (
        metadata.get("name")
        or token_info.get("name")
        or token_info.get("token_name")
        or item.get("name")
        or ""
    )

    symbol = (
        metadata.get("symbol")
        or token_info.get("symbol")
        or token_info.get("token_symbol")
        or item.get("symbol")
        or ""
    )

    if not name or str(name).strip().lower() in {"none", "null", "n/a"}:
        name = f"Token {_short(mint, 5)}"

    if not symbol or str(symbol).strip().lower() in {"none", "null", "n/a"}:
        symbol = "UNKNOWN"

    return str(name), str(symbol)


async def fetch_wallet_assets(
    wallet_address: str, max_assets: int = 100, priority: int = PRIORITY_LOW
) -> dict:
    """
    Fetch wallet fungible assets via standard Solana JSON-RPC (getBalance +
    getTokenAccountsByOwner) — full Helius -> QuickNode -> Alchemy -> dRPC
    failover via MultiRPCManager, no longer locked to Helius DAS.

    `priority` defaults to background priority (this feeds the Smart
    Wallet Intelligence engine's discovery/scoring/maintenance loops),
    keeping it from delaying user-triggered wallet balance/portfolio
    lookups which run at PRIORITY_HIGH in services/wallet_portfolio.py.

    Name/symbol/price aren't available from standard RPC the way they were
    from DAS — every token starts as ("Unknown"/"UNKNOWN", unpriced) and is
    filled in by the existing enrich_token_with_dexscreener() /
    enrich_wallet_tokens() pass below, exactly as it already did whenever
    DAS itself lacked metadata or pricing for a token.
    """
    max_assets = max(20, min(max_assets, 1000))

    try:
        holdings = await fetch_wallet_holdings(
            wallet_address,
            priority=priority,
            cache_key_prefix="wallet_intelligence",
            cache_ttl=HELIUS_WALLET_CACHE_TTL_SECONDS,
        )
    except Exception as e:
        logger.error(f"Wallet intelligence fetch error for {wallet_address}: {e}")
        return {
            "native_sol": 0.0,
            "tokens": [],
        }

    if holdings is None:
        logger.warning(f"Wallet intelligence holdings unavailable for {wallet_address} (all providers failed)")
        return {
            "native_sol": 0.0,
            "tokens": [],
        }

    tokens = []
    for token in holdings["tokens"][:max_assets]:
        mint = token["mint"]
        amount = token["amount"]

        if amount <= 0:
            continue

        # extract_token_name_symbol already degrades to "Token <mint
        # prefix>" / "UNKNOWN" when item/token_info are empty — exactly
        # what we pass here, since standard RPC has no name/symbol.
        name, symbol = extract_token_name_symbol({}, {}, mint)

        tokens.append({
            "mint": mint,
            "name": name,
            "symbol": symbol,
            "amount": amount,
            "price": 0.0,
            "value": 0.0,
            "priced": False,
        })

    logger.info(
        f"Wallet intelligence fetched {len(tokens)} tokens for {wallet_address}"
    )

    return {
        "native_sol": holdings["native_sol"],
        "tokens": tokens,
    }


async def enrich_token_with_dexscreener(token: dict) -> dict:
    """
    Use DexScreener to improve token name, symbol, price, and value.

    This runs even if Helius already returned metadata, because Helius
    metadata can sometimes be incomplete.
    """

    mint = token.get("mint")
    amount = _to_float(token.get("amount"))

    if not mint:
        return token

    try:
        token_data = await asyncio.wait_for(
            get_token_card_info(mint),
            timeout=6
        )

        if not token_data:
            return token

        ds_name = token_data.get("name")
        ds_symbol = token_data.get("symbol")
        ds_price = _to_float(token_data.get("price"))

        if ds_name and str(ds_name).strip():
            token["name"] = ds_name

        if ds_symbol and str(ds_symbol).strip():
            token["symbol"] = ds_symbol

        if ds_price > 0:
            token["price"] = ds_price
            token["value"] = amount * ds_price
            token["priced"] = True

    except asyncio.TimeoutError:
        logger.warning(f"DexScreener timeout pricing wallet token {mint}")
    except Exception as e:
        logger.warning(f"DexScreener pricing failed for wallet token {mint}: {e}")

    return token


async def enrich_wallet_tokens(tokens: list[dict], max_to_price: int = 30) -> list[dict]:
    """
    Improve wallet tokens using DexScreener.

    We enrich up to max_to_price tokens to avoid free API rate limits.
    """

    if not tokens:
        return []

    # Prioritize tokens with existing value or larger balances
    tokens.sort(
        key=lambda x: (
            _to_float(x.get("value")),
            _to_float(x.get("amount"))
        ),
        reverse=True
    )

    to_enrich = tokens[:max_to_price]
    untouched = tokens[max_to_price:]

    enriched = await asyncio.gather(
        *[enrich_token_with_dexscreener(token) for token in to_enrich],
        return_exceptions=False
    )

    final_tokens = enriched + untouched

    final_tokens.sort(
        key=lambda x: (
            _to_float(x.get("value")),
            _to_float(x.get("amount"))
        ),
        reverse=True
    )

    return final_tokens


def classify_wallet(
    total_usd: float,
    native_sol: float,
    token_count: int,
    priced_count: int,
    recent_tx_count: int,
) -> tuple[str, list[str]]:
    """
    Heuristic wallet classification.
    """

    notes = []

    if total_usd >= 1_000_000:
        primary = "🐳 Mega Whale"
        notes.append("Wallet value exceeds $1M.")
    elif total_usd >= 100_000:
        primary = "🐋 Whale"
        notes.append("Wallet value exceeds $100K.")
    elif total_usd >= 25_000:
        primary = "🦈 Large Holder"
        notes.append("Wallet value exceeds $25K.")
    elif total_usd >= 5_000:
        primary = "💼 Serious Holder"
        notes.append("Wallet value exceeds $5K.")
    elif total_usd >= 500:
        primary = "🧑‍💻 Retail Trader"
        notes.append("Moderate wallet value detected.")
    else:
        primary = "🌱 Small Wallet"
        notes.append("Low visible wallet value.")

    if native_sol >= 500:
        notes.append("High native SOL balance.")
    elif native_sol >= 100:
        notes.append("Strong native SOL balance.")

    if recent_tx_count >= 15 and total_usd >= 5_000:
        primary = "🧠 Possible Smart / Active Trader"
        notes.append("High recent activity with meaningful balance.")
    elif recent_tx_count >= 8:
        notes.append("Active trader behavior detected.")
    elif recent_tx_count == 0:
        notes.append("Low or no recent transaction activity.")

    if token_count >= 30:
        notes.append("Broad token exposure. Possible degen/collector wallet.")
    elif token_count >= 10:
        notes.append("Multiple token positions detected.")

    if token_count > 0 and priced_count / max(token_count, 1) < 0.35:
        notes.append("Many holdings are unpriced or illiquid.")

    return primary, notes[:4]


async def build_wallet_intelligence_card(wallet_address: str, limit: int = 10) -> str:
    """
    Build wallet intelligence card.

    Shows:
    - Native SOL balance
    - Estimated total value in USD/USDT
    - SOL equivalent
    - Top 10 token holdings
    - Wallet classification
    """

    wallet_address = wallet_address.strip().strip(",.;")
    limit = max(1, min(limit, 10))

    sol_price = await get_sol_price_usd()
    wallet_assets = await fetch_wallet_assets(wallet_address)

    native_sol = wallet_assets["native_sol"]
    tokens = wallet_assets["tokens"]

    enriched_tokens = await enrich_wallet_tokens(tokens)

    native_sol_value = native_sol * sol_price if sol_price > 0 else 0.0
    token_value = sum(_to_float(token.get("value")) for token in enriched_tokens)
    total_usd = native_sol_value + token_value

    total_sol_equivalent = total_usd / sol_price if sol_price > 0 else 0.0

    priced_count = sum(1 for token in enriched_tokens if token.get("priced"))
    token_count = len(enriched_tokens)

    try:
        recent_events = await get_recent_signatures(wallet_address, limit=20)
        recent_tx_count = len(recent_events)
    except Exception:
        recent_tx_count = 0

    classification, notes = classify_wallet(
        total_usd=total_usd,
        native_sol=native_sol,
        token_count=token_count,
        priced_count=priced_count,
        recent_tx_count=recent_tx_count,
    )

    solscan_url = f"https://solscan.io/account/{wallet_address}"

    text = (
        "👛 <b>Wallet Intelligence Card</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏷️ Type: <b>{classification}</b>\n"
        f"💰 Native SOL: <b>{format_token_amount(native_sol)} SOL</b>\n"
        f"💵 Native SOL Value: <b>{format_usd(native_sol_value)}</b>\n\n"
        f"💎 Total Estimated Value: <b>{format_usd(total_usd)}</b>\n"
        f"🟢 USDT Equivalent: <b>{format_usd(total_usd)} USDT</b>\n"
        f"◎ SOL Equivalent: <b>{format_token_amount(total_sol_equivalent)} SOL</b>\n\n"
        f"🪙 Tokens Held: <b>{token_count}</b>\n"
        f"✅ Priced Tokens: <b>{priced_count}</b>\n"
        f"⚪ Unpriced Tokens: <b>{max(token_count - priced_count, 0)}</b>\n"
        f"⚡ Recent Tx Count: <b>{recent_tx_count}</b>\n\n"
    )

    if notes:
        text += "🧠 <b>Wallet Profile Notes</b>\n"
        for note in notes:
            text += f"• {_esc(note)}\n"
        text += "\n"

    if enriched_tokens:
        text += "📦 <b>Top Holdings</b>\n"

        for index, token in enumerate(enriched_tokens[:limit], 1):
            mint = token.get("mint", "")
            name = token.get("name") or f"Token {_short(mint, 5)}"
            symbol = token.get("symbol") or "UNKNOWN"
            amount = token.get("amount", 0)
            price = token.get("price", 0)
            value = token.get("value", 0)
            priced = token.get("priced", False)

            text += (
                f"<b>{index}. {_esc(name)} ({_esc(symbol)})</b>\n"
                f"   🪙 Amount: <b>{format_token_amount(amount)}</b>\n"
                f"   💵 Price: <b>{format_price(price) if priced else 'Unpriced'}</b>\n"
                f"   💰 Value: <b>{format_usd(value) if priced else 'Unpriced'}</b>\n"
                f"   📝 Mint: <code>{_esc(_short(mint, 9))}</code>\n\n"
            )
    else:
        text += "📭 <b>No fungible token holdings detected.</b>\n\n"

    text += (
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔎 <a href=\"{solscan_url}\">View Wallet on Solscan</a>\n\n"
        f"<code>{wallet_address}</code>\n\n"
        "⚠️ <i>Classification is heuristic. Unpriced tokens may have no public market.</i>\n"
        "⚡ Powered by AlphaPulse"
    )

    return text
