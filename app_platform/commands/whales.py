from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from domain.intelligence.whale_tracker import (
    add_tracked_wallet,
    remove_tracked_wallet,
    get_tracked_wallets,
    get_wallet_activity,
    format_wallet_activity,
    format_all_wallets,
)

router = Router()


@router.message(Command("track"))
async def cmd_track(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ <b>Usage:</b> /track &lt;wallet_address&gt;\n\n"
            "Example:\n"
            "<code>/track 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU</code>"
        )
        return

    wallet_address = args[1].strip()

    # Basic Solana address validation (32-44 alphanumeric characters)
    if not (32 <= len(wallet_address) <= 44 and wallet_address.replace(" ", "").isalnum()):
        await message.answer("❌ Invalid Solana wallet address.")
        return

    success = await add_tracked_wallet(
        user_id=message.from_user.id,
        wallet_address=wallet_address,
        label="Whale"
    )

    if success:
        await message.answer(
            f"✅ <b>Wallet Added</b>\n\n"
            f"📝 <code>{wallet_address}</code>\n\n"
            f"Use /wallets to see all tracked wallets.\n"
            f"Use /activity &lt;wallet&gt; to see recent transactions."
        )
    else:
        await message.answer("⚠️ This wallet is already being tracked.")


@router.message(Command("untrack"))
async def cmd_untrack(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ <b>Usage:</b> /untrack &lt;wallet_address&gt;")
        return

    wallet_address = args[1].strip()
    success = await remove_tracked_wallet(message.from_user.id, wallet_address)

    if success:
        await message.answer(f"🗑️ <b>Stopped tracking:</b>\n<code>{wallet_address}</code>")
    else:
        await message.answer("⚠️ Wallet not found in your tracked list.")


@router.message(Command("wallets"))
async def cmd_wallets(message: Message):
    wallets = await get_tracked_wallets(message.from_user.id)
    report = format_all_wallets(wallets)
    await message.answer(report)


@router.message(Command("activity"))
async def cmd_activity(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ <b>Usage:</b> /activity &lt;wallet_address&gt;\n\n"
            "Shows the last 5 transactions for any Solana wallet."
        )
        return

    wallet_address = args[1].strip()
    await message.answer("🔍 Fetching wallet activity...")

    transactions = await get_wallet_activity(wallet_address, limit=5)
    report = format_wallet_activity(wallet_address, transactions)
    await message.answer(report)
