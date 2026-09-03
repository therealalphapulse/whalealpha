from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from providers.marketdata.geckoterminal import get_trending_tokens as get_gt_trending_tokens
from providers.marketdata.dexscreener import get_trending_tokens as get_ds_trending_tokens

router = Router()


def format_number(value) -> str:
    """Format large numbers into readable strings."""
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


def format_change(value) -> str:
    """Format price change with emoji."""
    try:
        num = float(value)
        emoji = "🟢" if num > 0 else "🔴" if num < 0 else "⚪"
        return f"{emoji} {num:.1f}%"
    except (ValueError, TypeError):
        return "⚪ N/A"


@router.message(Command("trending"))
async def cmd_trending(message: Message):
    await message.answer("🔥 Scanning trending Solana tokens...")

    # Primary source: GeckoTerminal (better true trending signal)
    tokens = await get_gt_trending_tokens()
    source = "GeckoTerminal"

    # Fallback source: DexScreener
    if not tokens:
        tokens = await get_ds_trending_tokens()
        source = "DexScreener (fallback)"

    if not tokens:
        await message.answer("⚠️ Could not fetch trending data. Try again later.")
        return

    text = (
        "🔥 <b>Trending Solana Tokens</b>\n"
        f"📡 Source: {source}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    for i, token in enumerate(tokens[:10], 1):
        name = token.get("name", "Unknown")
        symbol = token.get("symbol", "???")
        price = token.get("price", "N/A")
        change = token.get("price_change_24h", "N/A")
        volume = token.get("volume_24h", "N/A")
        liquidity = token.get("liquidity", "N/A")
        contract = token.get("contract", "")

        text += (
            f"<b>{i}. {name} ({symbol})</b>\n"
            f"   💰 Price: ${price}\n"
            f"   📈 24h: {format_change(change)}\n"
            f"   📊 Vol: {format_number(volume)}\n"
            f"   💧 Liq: {format_number(liquidity)}\n"
        )

        if contract:
            text += f"   📝 <code>{contract}</code>\n"

        text += "\n"

    text += "━━━━━━━━━━━━━━━━━━━━━\n⚡ Powered by AlphaPulse"

    await message.answer(text)
