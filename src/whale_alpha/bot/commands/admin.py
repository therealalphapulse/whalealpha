"""Admin-only whale-wallet management commands — port of src/bot/commands/admin.ts.

Every mutation routes through WhaleWalletAdminService, which re-checks the
actor's role independently of this bot-layer gate (defense in depth — never
trust a single layer of RBAC). See porting requirement #5.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from whale_alpha.db.models import Role, User
from whale_alpha.integrations.solana_connection import is_valid_solana_address
from whale_alpha.services.admin.whale_wallet_admin_service import Actor, WhaleWalletAdminService

router = Router(name="admin")


async def _actor_for(session_factory: async_sessionmaker[AsyncSession], telegram_id: str) -> Actor:
    async with session_factory() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id, role=Role.ADMIN)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return Actor(id=user.id, role=user.role)


def register_admin_commands(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    @router.message(Command("addwhale"))
    async def addwhale_handler(message: Message, is_admin: bool = False) -> None:
        if not is_admin:
            await message.answer("⛔ This command is restricted to administrators.")
            return

        args = (message.text or "").split(maxsplit=2)[1:]  # drop the "/addwhale" token
        address = args[0] if args else None
        label = args[1] if len(args) > 1 else None

        if not address or not is_valid_solana_address(address):
            await message.answer(
                "Usage: /addwhale <solana_address> [label]\nAddress must be a valid Solana public key."
            )
            return

        if message.from_user is None:
            return
        actor = await _actor_for(session_factory, str(message.from_user.id))
        async with session_factory() as session:
            service = WhaleWalletAdminService(session)
            wallet = await service.add_wallet(actor, address, label)
        await message.answer(
            f"✅ Added wallet `{address}` with status PENDING_REVIEW (id: {wallet.id}).",
            parse_mode="Markdown",
        )

    @router.message(Command("approvewhale"))
    async def approvewhale_handler(message: Message, is_admin: bool = False) -> None:
        if not is_admin:
            await message.answer("⛔ This command is restricted to administrators.")
            return

        parts = (message.text or "").split(maxsplit=1)
        wallet_id = parts[1].strip() if len(parts) > 1 else None
        if not wallet_id:
            await message.answer("Usage: /approvewhale <wallet_id>")
            return

        if message.from_user is None:
            return
        actor = await _actor_for(session_factory, str(message.from_user.id))
        async with session_factory() as session:
            service = WhaleWalletAdminService(session)
            from whale_alpha.db.models import WalletStatus

            await service.set_status(actor, wallet_id, WalletStatus.APPROVED)
        await message.answer(f"✅ Wallet {wallet_id} approved.")

    @router.message(Command("removewhale"))
    async def removewhale_handler(message: Message, is_admin: bool = False) -> None:
        if not is_admin:
            await message.answer("⛔ This command is restricted to administrators.")
            return

        parts = (message.text or "").split(maxsplit=1)
        wallet_id = parts[1].strip() if len(parts) > 1 else None
        if not wallet_id:
            await message.answer("Usage: /removewhale <wallet_id>")
            return

        if message.from_user is None:
            return
        actor = await _actor_for(session_factory, str(message.from_user.id))
        async with session_factory() as session:
            service = WhaleWalletAdminService(session)
            await service.remove(actor, wallet_id)
        await message.answer(f"🗑️ Wallet {wallet_id} removed.")

    return router
