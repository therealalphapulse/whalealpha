from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from providers.marketdata.dexscreener import get_token_info

router = Router()


def format_number(value) -> str:
    try:
        num = float(value)
        if num >= 1_000_000_000:
            return f"${num / 1_000_000_000:.2f}B"
        elif num >= 1_000_000:
            return f"${num / 1_000_000:.2f}M"
        elif num >= 1_000:
            return f"${num / 1_000:.2f}K"
        else:
            return f"${num:,.6f}"
    except (ValueError, TypeError):
        return str(value)


def format_change(value) -> str:
    try:
        num = float(value)
        emoji = "🟢" if num > 0 else "🔴" if num < 0 else "⚪"
        return f"{emoji} {num:.1f}%"
    except (ValueError, TypeError):
        return "⚪ N/A"


@router.message(Command("token"))
async def cmd_token(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ <b>Usage:</b> /token &lt;contract_address&gt;\n\n"
            "Example:\n"
            "<code>/token So11111111111111111111111111111111111111112</code>"
        )
        return

    contract = args[1].strip()
    await message.answer(f"📡 Scanning token...")

    data = await get_token_info(contract)

    if not data:
        await message.answer(
            "⚠️ Token not found.\n\n"
            "Make sure you're using a valid Solana contract address."
        )
        return

    text = (
        f"🔍 <b>Token Report</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📛 <b>{data['name']}</b> ({data['symbol']})\n\n"
        f"💰 <b>Price:</b> ${data['price']}\n"
        f"📈 <b>5m Change:</b> {format_change(data['price_change_5m'])}\n"
        f"📈 <b>1h Change:</b> {format_change(data['price_change_1h'])}\n"
        f"📈 <b>24h Change:</b> {format_change(data['price_change_24h'])}\n\n"
        f"📊 <b>24h Volume:</b> {format_number(data['volume_24h'])}\n"
        f"💧 <b>Liquidity:</b> {format_number(data['liquidity'])}\n"
        f"🏦 <b>Market Cap:</b> {format_number(data['market_cap'])}\n"
        f"💎 <b>FDV:</b> {format_number(data['fdv'])}\n\n"
        f"🔗 <b>DEX:</b> {data['dex']}\n"
        f"📝 <b>Contract:</b>\n<code>{data['contract']}</code>\n\n"
    )

    if data.get("pair_url"):
        text += f"🔗 <a href=\"{data['pair_url']}\">View on DexScreener</a>\n\n"

    text += "━━━━━━━━━━━━━━━━━━━━━\n⚡ Powered by AlphaPulse"

    await message.answer(text)
