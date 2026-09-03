from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from providers.marketdata.goplus import check_token_security, format_security_report

router = Router()


@router.message(Command("security"))
async def cmd_security(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ <b>Usage:</b> /security &lt;contract_address&gt;\n\n"
            "Example:\n"
            "<code>/security So11111111111111111111111111111111111111112</code>"
        )
        return

    contract = args[1].strip()
    await message.answer("🔒 Running security scan...")

    data = await check_token_security(contract)

    if not data:
        await message.answer(
            "⚠️ Could not fetch security data.\n\n"
            "Possible reasons:\n"
            "• Invalid contract address\n"
            "• Token not indexed by GoPlus\n"
            "• API temporarily unavailable"
        )
        return

    report = format_security_report(data, contract)
    await message.answer(report)
