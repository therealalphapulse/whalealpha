from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from domain.intelligence.narrative_scanner import scan_narratives, format_narrative_report

router = Router()


@router.message(Command("narratives"))
async def cmd_narratives(message: Message):
    await message.answer("🔍 Scanning token narratives...")

    narratives = await scan_narratives()

    report = format_narrative_report(narratives)
    await message.answer(report)
