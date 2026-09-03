import re
import logging

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from domain.intelligence.wallet_portfolio import build_wallet_portfolio_report
from providers.marketdata.dexscreener import get_token_card_info

router = Router()
logger = logging.getLogger("AlphaPulse.WalletPortfolioCommand")

SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def is_valid_solana_address(address: str) -> bool:
    return bool(SOLANA_ADDRESS_RE.fullmatch(address))


@router.message(Command("wallet_portfolio", "wportfolio", "wp"))
async def cmd_wallet_portfolio(message: Message):
    try:
        parts = message.text.split()

        if len(parts) < 2:
            await message.answer(
                "⚠️ <b>Usage:</b>\n"
                "<code>/wallet_portfolio &lt;wallet_address&gt; [limit]</code>\n\n"
                "Shortcuts:\n"
                "<code>/wportfolio &lt;wallet_address&gt;</code>\n"
                "<code>/wp &lt;wallet_address&gt;</code>\n\n"
                "Example:\n"
                "<code>/wp YOUR_PHANTOM_WALLET_ADDRESS</code>\n\n"
                "💡 If you have a token contract, paste it directly instead."
            )
            return

        wallet_address = parts[1].strip()

        if not is_valid_solana_address(wallet_address):
            await message.answer("❌ Invalid Solana wallet address.")
            return

        limit = 10

        if len(parts) >= 3:
            try:
                limit = int(parts[2])
            except ValueError:
                await message.answer("❌ Limit must be a number.")
                return

        limit = max(1, min(limit, 15))

        await message.answer("💼 Fetching wallet portfolio snapshot...")

        report = await build_wallet_portfolio_report(
            wallet_address=wallet_address,
            limit=limit
        )

        # If Helius found no wallet tokens, check whether the address is actually a token contract.
        if report.startswith("📭"):
            token_data = await get_token_card_info(wallet_address)

            if token_data:
                await message.answer(
                    "ℹ️ <b>This looks like a token contract, not a wallet.</b>\n\n"
                    f"📛 <b>{token_data.get('name', 'Unknown')}</b> "
                    f"({token_data.get('symbol', '???')})\n\n"
                    "For token analysis, use:\n"
                    f"<code>/token {wallet_address}</code>\n\n"
                    "Or paste the contract directly without any command for the instant scan.\n\n"
                    "For wallet portfolio snapshots, use your Phantom/Solflare public wallet address."
                )
                return

        await message.answer(report)

    except Exception as e:
        logger.error(f"/wp command error: {e}")

        await message.answer(
            "⚠️ <b>Wallet Snapshot Failed</b>\n\n"
            "Something went wrong while fetching this wallet.\n"
            "Please try again shortly."
        )
