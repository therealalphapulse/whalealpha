from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from domain.admin.user_service import add_to_watchlist, remove_from_watchlist, get_user_watchlist

router = Router()


@router.message(Command("add"))
async def cmd_add(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ <b>Usage:</b> /add &lt;contract_address&gt;")
        return

    contract = args[1].strip()
    
    # Basic Solana address validation (32-44 alphanumeric characters)
    if not (32 <= len(contract) <= 44 and contract.isalnum()):
        await message.answer("❌ Invalid Solana contract address.")
        return

    success = await add_to_watchlist(message.from_user.id, contract)
    
    if success:
        await message.answer(
            f"✅ <b>Added to Watchlist</b>\n\n"
            f"📝 <code>{contract}</code>\n\n"
            f"Use /watchlist to see your tracked tokens."
        )
    else:
        await message.answer("⚠️ This token is already in your watchlist.")


@router.message(Command("remove"))
async def cmd_remove(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ <b>Usage:</b> /remove &lt;contract_address&gt;")
        return

    contract = args[1].strip()
    success = await remove_from_watchlist(message.from_user.id, contract)
    
    if success:
        await message.answer(f"🗑️ <b>Removed from Watchlist:</b>\n<code>{contract}</code>")
    else:
        await message.answer("⚠️ Token not found in your watchlist.")


@router.message(Command("watchlist"))
async def cmd_watchlist(message: Message):
    items = await get_user_watchlist(message.from_user.id)
    
    if not items:
        await message.answer(
            "📭 <b>Your Watchlist is Empty</b>\n\n"
            "Use <code>/add &lt;contract&gt;</code> to start tracking tokens."
        )
        return

    text = "👁️ <b>Your Watchlist</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    for i, item in enumerate(items, 1):
        text += f"<b>{i}.</b> <code>{item.contract}</code>\n"
        
    text += f"\n━━━━━━━━━━━━━━━━━━━━━\n📊 Total: {len(items)} tokens"
    await message.answer(text)
