"""/connectwallet, /disconnectwallet — did not exist at all before this port,
despite being referenced in a message string in trading.py and being a
prerequisite for every other trading feature.

SECURITY NOTE (read this before using in production): Telegram is not a
secure channel for pasting a private key. The message containing the raw key
is sent in plaintext to Telegram's servers before this bot ever sees it, and
Telegram retains message history independent of whether we delete it
client-side. This flow best-effort-deletes the user's message immediately
after processing it and instructs the user to do the same if that fails, but
that only removes it from the visible chat — it does not undo the exposure.
The `docs/SECURITY.md` guidance to prefer non-custodial, client-side signing
over pasting a raw key into a bot applies with full force here; this
implementation is the "if custodial signing is unavoidable" path referenced
in integrations/jupiter_client.py, not the recommended one.

Accepts either:
  * a base58-encoded 64-byte secret key (the format `solana-keygen` /
    Phantom's "export private key" produce), or
  * a JSON array of 64 integers (the format a Solana CLI keypair file uses).
"""

from __future__ import annotations

import contextlib
import json

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from solders.keypair import Keypair
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from whale_alpha.config import Env
from whale_alpha.db.models import Role, User
from whale_alpha.utils.logger import child_logger
from whale_alpha.utils.security.encryption import encrypt_secret, serialize_encrypted

router = Router(name="wallet")
log = child_logger("walletCommands")


class ConnectWalletStates(StatesGroup):
    waiting_for_key = State()


def _parse_secret_key(text: str) -> bytes | None:
    text = text.strip()
    if not text:
        return None

    # JSON array format, e.g. "[12,45,...]" (64 ints).
    if text.startswith("["):
        try:
            values = json.loads(text)
            if isinstance(values, list) and len(values) == 64 and all(isinstance(v, int) for v in values):
                return bytes(values)
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    # base58 secret key (Phantom / solana-keygen export format).
    try:
        keypair = Keypair.from_base58_string(text)
        return bytes(keypair)
    except Exception:  # noqa: BLE001 — any parse failure means "not a valid key"
        return None


async def _get_or_create_user(session_factory: async_sessionmaker, telegram_id: str) -> User:
    async with session_factory() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id, role=Role.USER)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


def register_wallet_commands(session_factory: async_sessionmaker, env: Env) -> Router:
    @router.message(Command("connectwallet"))
    async def connectwallet_handler(message: Message, state: FSMContext) -> None:
        if message.chat.type != "private":
            await message.answer("⚠️ For your safety, use /connectwallet in a private DM with this bot, not a group.")
            return

        await state.set_state(ConnectWalletStates.waiting_for_key)
        await message.answer(
            "🔐 *Connect your wallet*\n\n"
            "Reply with your Solana private key — either the base58 string "
            "(from Phantom's \"Export Private Key\") or a JSON array of 64 "
            "numbers (a Solana CLI keypair file's contents).\n\n"
            "⚠️ *This bot will hold custody of this key to sign trades on your "
            "behalf.* Only do this with a wallet you're comfortable trading "
            "programmatically with — never your main holdings. I'll try to "
            "delete your message immediately after processing it, but please "
            "delete it yourself too if that fails.\n\n"
            "Send /cancel to back out.",
            parse_mode="Markdown",
        )

    @router.message(Command("cancel"), StateFilter(ConnectWalletStates.waiting_for_key))
    async def cancel_handler(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Cancelled. No key was stored.")

    @router.message(StateFilter(ConnectWalletStates.waiting_for_key), F.text)
    async def receive_key_handler(message: Message, state: FSMContext) -> None:
        raw_text = message.text or ""
        telegram_id = str(message.from_user.id)

        secret_bytes = _parse_secret_key(raw_text)

        # Best-effort scrub of the plaintext key from the visible chat
        # regardless of whether parsing succeeded.
        with contextlib.suppress(Exception):  # noqa: BLE001 — deletion is best-effort
            await message.delete()

        if secret_bytes is None:
            await message.answer(
                "❌ That didn't parse as a valid private key (base58 string or "
                "JSON array of 64 numbers). Please try again, or /cancel.\n\n"
                "_(Your message was deleted either way — please double-check "
                "it's actually gone.)_",
                parse_mode="Markdown",
            )
            return

        try:
            keypair = Keypair.from_bytes(secret_bytes)
        except Exception:  # noqa: BLE001 — any construction failure means "not a valid keypair"
            await message.answer("❌ That key parsed but isn't a valid Solana keypair. Please try again, or /cancel.")
            return

        public_key = str(keypair.pubkey())
        encrypted = serialize_encrypted(encrypt_secret(secret_bytes.hex(), env))

        user = await _get_or_create_user(session_factory, telegram_id)
        async with session_factory() as session:
            db_user = await session.get(User, user.id)
            db_user.encrypted_wallet_key = encrypted
            db_user.wallet_public_key = public_key
            await session.commit()

        await state.clear()
        short = f"{public_key[:4]}...{public_key[-4:]}"
        await message.answer(
            f"✅ Wallet connected: `{short}`\n\n"
            "You can now use /buy and /sell, or set up /autotrading rules.\n"
            "Use /disconnectwallet any time to remove this key.",
            parse_mode="Markdown",
        )

    @router.message(Command("disconnectwallet"))
    async def disconnectwallet_handler(message: Message) -> None:
        telegram_id = str(message.from_user.id)
        async with session_factory() as session:
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()
            if user is None or not user.wallet_public_key:
                await message.answer("No wallet connected.")
                return
            user.encrypted_wallet_key = None
            user.wallet_public_key = None
            await session.commit()

        await message.answer(
            "🔌 Wallet disconnected and key deleted from the database. "
            "Auto Trading (if it was enabled) will no longer be able to execute trades."
        )

    return router
