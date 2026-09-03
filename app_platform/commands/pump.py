from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from domain.admin.user_service import get_or_create_user
from domain.signals.pump_radar import set_pump_subscription, get_pump_subscription_status

router = Router()

@router.message(Command("pump_alerts_on"))
async def cmd_pump_alerts_on(message: Message):
    await get_or_create_user(telegram_id=message.from_user.id, username=message.from_user.username, first_name=message.from_user.first_name)
    await set_pump_subscription(message.from_user.id, True)
    await message.answer("✅ Alerts Enabled. You will receive high-quality Pump.fun signals.")

@router.message(Command("pump_alerts_off"))
async def cmd_pump_alerts_off(message: Message):
    await set_pump_subscription(message.from_user.id, False)
    await message.answer("🔕 Alerts Disabled.")

@router.message(Command("pump_status"))
async def cmd_pump_status(message: Message):
    status = "✅ Active" if await get_pump_subscription_status(message.from_user.id) else "🔕 Disabled"
    await message.answer(f"Radar Status: {status}")
