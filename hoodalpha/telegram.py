"""
hoodalpha/telegram.py

HoodAlpha's own aiogram Bot instance, built from HOOD_BOT_TOKEN --
never WhaleAlpha's BOT_TOKEN/TELEGRAM_BOT_TOKEN. Used by both
hoodalpha/worker.py (to send HoodAlert cards) and hoodalpha/gateway.py
(the user-facing /start /status commands).
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from hoodalpha.settings import HOOD_BOT_TOKEN, HOOD_TELEGRAM_CHAT_IDS

logger = logging.getLogger("HoodAlpha.Telegram")


def build_bot() -> Bot:
    if not HOOD_BOT_TOKEN:
        raise ValueError(
            "HOOD_BOT_TOKEN is missing. HoodAlpha needs its own BotFather "
            "bot/token -- it must never reuse WhaleAlpha's BOT_TOKEN."
        )
    return Bot(token=HOOD_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def format_hood_alert(watch, alert_price: float, rise_pct: float) -> str:
    symbol = watch.source_symbol or "?"
    name = watch.source_name or watch.contract[:10]
    return (
        f"🟢 <b>HOODALPHA — DIP RECOVERY</b>\n\n"
        f"<b>{name}</b> (${symbol})\n"
        f"Chain: <code>{watch.chain}</code>\n"
        f"Contract: <code>{watch.contract}</code>\n\n"
        f"Entry (WhaleAlpha alert): <code>${watch.entry_alert_price:.8f}</code>\n"
        f"Lowest dip: <code>${watch.lowest_dip_price:.8f}</code>\n"
        f"Now: <code>${alert_price:.8f}</code>\n\n"
        f"📈 Up <b>{rise_pct:.1f}%</b> from the dip bottom."
    )


async def broadcast(bot: Bot, text: str) -> int:
    """Sends to every configured HOOD_TELEGRAM_CHAT_IDS destination.
    Returns the number of successful sends (mirrors WhaleAlpha's own
    per-subscriber delivery-confirmation pattern -- a send is never
    assumed to have succeeded just because it didn't raise)."""
    sent = 0
    for chat_id in HOOD_TELEGRAM_CHAT_IDS:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            sent += 1
        except Exception as e:
            logger.warning("HoodAlpha send failed for chat_id=%s: %s", chat_id, e)
    return sent
