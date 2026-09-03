from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from providers.marketdata.coingecko import get_solana_price, get_global_market
from providers.marketdata.dexscreener import get_market_overview

router = Router()


@router.message(Command("market"))
async def cmd_market(message: Message):
    await message.answer("📡 Fetching market data...")

    sol = await get_solana_price()
    global_data = await get_global_market()
    dex_data = await get_market_overview()

    if not sol:
        await message.answer("⚠️ Failed to fetch market data. Try again later.")
        return

    change_emoji = "🟢" if (sol.get("change_24h") or 0) > 0 else "🔴"
    change_val = sol.get("change_24h", 0) or 0

    text = (
        "📊 <b>AlphaPulse Market Overview</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"☀️ <b>Solana (SOL)</b>\n"
        f"💰 Price: <b>${sol['price']:,.2f}</b>\n"
        f"{change_emoji} 24h Change: <b>{change_val:.2f}%</b>\n"
        f"📈 24h Volume: <b>${sol['volume_24h']:,.0f}</b>\n"
        f"🏦 Market Cap: <b>${sol['market_cap']:,.0f}</b>\n\n"
    )

    if global_data:
        text += (
            "🌍 <b>Global Crypto Market</b>\n"
            f"💎 Total Cap: <b>${global_data['total_market_cap']:,.0f}</b>\n"
            f"📊 24h Volume: <b>${global_data['total_volume']:,.0f}</b>\n"
            f"₿ BTC Dominance: <b>{global_data['btc_dominance']:.1f}%</b>\n\n"
        )

    if dex_data:
        text += (
            "🔗 <b>Solana DEX Activity</b>\n"
            f"📊 Pairs Scanned: <b>{dex_data['pairs_scanned']}</b>\n"
            f"💰 DEX Volume: <b>${dex_data['total_volume']:,.0f}</b>\n"
            f"💧 DEX Liquidity: <b>${dex_data['total_liquidity']:,.0f}</b>\n"
            f"🟢 Gainers: <b>{dex_data['gainers']}</b>\n"
            f"🔴 Losers: <b>{dex_data['losers']}</b>\n"
            f"📡 Sentiment: <b>{dex_data['sentiment']}</b>\n"
        )

    text += "\n━━━━━━━━━━━━━━━━━━━━━\n⚡ Powered by AlphaPulse"

    await message.answer(text)
