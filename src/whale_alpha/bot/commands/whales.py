"""/whales — port of src/bot/commands/whales.ts.

Browse the admin-curated elite wallet database. Read-only: users can view
rankings and stats but have no path to add, edit, or remove entries from here.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from whale_alpha.db.models import WalletStatus, WhaleWallet

router = Router(name="whales")


def _short(address: str) -> str:
    return f"{address[:4]}...{address[-4:]}"


def register_whales_command(session_factory: async_sessionmaker) -> Router:
    @router.message(Command("whales"))
    async def whales_handler(message: Message) -> None:
        async with session_factory() as session:
            result = await session.execute(
                select(WhaleWallet)
                .where(WhaleWallet.status == WalletStatus.APPROVED)
                .order_by(WhaleWallet.score.desc())
                .limit(10)
            )
            wallets = list(result.scalars())

        if not wallets:
            await message.answer("No approved whale wallets yet. Check back soon.")
            return

        lines = []
        for i, w in enumerate(wallets):
            roi = f"{w.roi_30d * 100:.1f}%" if w.roi_30d is not None else "n/a"
            win_rate = f"{w.win_rate * 100:.0f}%" if w.win_rate is not None else "n/a"
            lines.append(f"{i + 1}. `{_short(w.address)}` — score {w.score:.0f} · 30d ROI {roi} · win rate {win_rate}")

        await message.answer(
            "🐋 *Top Whale Wallets*\n\n"
            + "\n".join(lines)
            + "\n\n_Wallet list is admin-curated. Use /wallet <rank> for details._",
            parse_mode="Markdown",
        )

    return router
