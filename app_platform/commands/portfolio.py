from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.filters import Command

from domain.admin.user_service import get_or_create_user
from domain.intelligence.portfolio import (
    add_or_update_position,
    remove_position,
    build_portfolio_report,
    format_price,
    format_token_amount,
)
from app_platform.keyboards.portfolio import portfolio_hub_row
from app_platform.commands.paper_trading import build_paper_dashboard

router = Router()


def is_valid_solana_address(address: str) -> bool:
    return 32 <= len(address) <= 44 and address.isalnum()


@router.message(Command("portfolio_add", "padd"))
async def cmd_portfolio_add(message: Message):
    parts = message.text.split()

    if len(parts) < 3:
        await message.answer(
            "⚠️ <b>Usage:</b>\n"
            "<code>/portfolio_add &lt;contract&gt; &lt;token_amount&gt; [entry_price]</code>\n\n"
            "Examples:\n"
            "<code>/portfolio_add DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 1000000</code>\n\n"
            "<code>/portfolio_add DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 1000000 0.000021</code>\n\n"
            "Shortcut:\n"
            "<code>/padd &lt;contract&gt; &lt;amount&gt;</code>"
        )
        return

    contract = parts[1].strip()

    if not is_valid_solana_address(contract):
        await message.answer("❌ Invalid Solana token contract address.")
        return

    try:
        token_amount = float(parts[2])
    except ValueError:
        await message.answer("❌ Token amount must be a number.")
        return

    entry_price = None

    if len(parts) >= 4:
        try:
            entry_price = float(parts[3])
        except ValueError:
            await message.answer("❌ Entry price must be a number.")
            return

    await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    await message.answer("💼 Updating portfolio position...")

    result = await add_or_update_position(
        user_id=message.from_user.id,
        contract=contract,
        token_amount=token_amount,
        entry_price=entry_price,
    )

    if not result.get("ok"):
        await message.answer(f"⚠️ {result.get('message', 'Could not add position.')}")
        return

    action_text = "Added new position" if result["action"] == "created" else "Updated existing position"

    await message.answer(
        f"✅ <b>{action_text}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📛 <b>{result['name']}</b> ({result['symbol']})\n"
        f"🪙 Amount: <b>{format_token_amount(result['amount'])}</b>\n"
        f"🎯 Entry Price: <b>{format_price(result['entry_price'])}</b>\n"
        f"💵 Current Price: <b>{format_price(result['current_price'])}</b>\n\n"
        f"Use /portfolio to view your full portfolio."
    )


@router.message(Command("portfolio"))
async def cmd_portfolio(message: Message):
    await message.answer("📊 Calculating portfolio value...")

    report = await build_portfolio_report(message.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[portfolio_hub_row("real")])
    await message.answer(report, reply_markup=keyboard)


@router.callback_query(F.data == "portfolio_hub:real")
async def cb_portfolio_hub_real(callback: CallbackQuery):
    await callback.answer()

    report = await build_portfolio_report(callback.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[portfolio_hub_row("real")])
    await callback.message.answer(report, reply_markup=keyboard)


@router.callback_query(F.data == "portfolio_hub:paper")
async def cb_portfolio_hub_paper(callback: CallbackQuery):
    await callback.answer()

    text, keyboard = await build_paper_dashboard(callback.from_user.id)
    await callback.message.answer(text, reply_markup=keyboard)


@router.message(Command("portfolio_remove", "premove"))
async def cmd_portfolio_remove(message: Message):
    parts = message.text.split()

    if len(parts) < 2:
        await message.answer(
            "⚠️ <b>Usage:</b>\n"
            "<code>/portfolio_remove &lt;contract&gt;</code>\n\n"
            "Shortcut:\n"
            "<code>/premove &lt;contract&gt;</code>"
        )
        return

    contract = parts[1].strip()

    success = await remove_position(
        user_id=message.from_user.id,
        contract=contract,
    )

    if success:
        await message.answer(
            f"🗑️ <b>Removed from portfolio</b>\n\n"
            f"<code>{contract}</code>"
        )
    else:
        await message.answer("⚠️ This token was not found in your portfolio.")
