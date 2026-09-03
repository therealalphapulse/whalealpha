from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from domain.signals.signal_tracker import (
    build_active_signals_report,
    build_winners_report,
    build_signal_status_report,
    build_top_signals_card,
    get_cached_alltime_trending,
)

router = Router()


@router.message(Command("signals"))
async def cmd_signals(message: Message):
    report = await build_active_signals_report()
    await message.answer(report)


@router.message(Command("winners"))
async def cmd_winners(message: Message):
    report = await build_winners_report()
    await message.answer(report)


@router.message(Command("alltime"))
async def cmd_alltime_trending(message: Message):
    report = await get_cached_alltime_trending()
    await message.answer(report)


@router.message(Command("signal_status"))
async def cmd_signal_status(message: Message):
    report = await build_signal_status_report()
    await message.answer(report)


@router.message(Command("top"))
async def cmd_top_signals(message: Message):
    report = await build_top_signals_card(limit=5)
    await message.answer(report)


@router.message(Command("top10"))
async def cmd_top10_signals(message: Message):
    report = await build_top_signals_card(limit=10)
    await message.answer(report)
