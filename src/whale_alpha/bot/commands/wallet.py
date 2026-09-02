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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from whale_alpha.config import Env
from whale_alpha.integrations.solana_connection import get_sol_balance
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


async def _get_or_create_user(session_factory: async_sessionmaker[AsyncSession], telegram_id: str) -> User:
    async with session_factory() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id, role=Role.USER)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


def _short_address(address: str, left: int = 6, right: int = 6) -> str:
    if len(address) <= left + right + 3:
        return address
    return f"{address[:left]}...{address[-right:]}"


def _format_amount(value: float) -> str:
    if value >= 1_000_000:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if value == 0:
        return "0"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _wallet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 BUY", callback_data="wallet:buy"), InlineKeyboardButton(text="💸 SELL", callback_data="wallet:sell")],
        [InlineKeyboardButton(text="🔄 REFRESH", callback_data="wallet:refresh"), InlineKeyboardButton(text="📊 PORTFOLIO", callback_data="wallet:portfolio")],
        [InlineKeyboardButton(text="🔌 DISCONNECT", callback_data="wallet:disconnect")],
    ])


def _wallet_card(address: str, sol_balance: float, sol_usd: float | None, holdings: list[tuple[str, float]], *, error: str | None = None) -> str:
    lines = ["👛 <b>YOUR WALLET</b>", "", "🟢 <b>Connected</b>", f"<code>{_short_address(address)}</code>", ""]
    if sol_usd is None:
        lines.append(f"💰 <b>{_format_amount(sol_balance)} SOL</b>")
    else:
        lines.append(f"💰 <b>{_format_amount(sol_balance)} SOL</b>  ≈ <b>${sol_usd:,.2f}</b>")
    lines.extend([f"🪙 <b>{len(holdings)} token holdings</b>", ""])
    if holdings:
        for mint, amount in holdings[:8]:
            lines.append(f"• <code>{_short_address(mint, 4, 4)}</code>  <b>{_format_amount(amount)}</b>")
        if len(holdings) > 8:
            lines.append(f"• … and {len(holdings) - 8} more")
    else:
        lines.append("No SPL tokens found.")
    if error:
        lines.extend(["", f"⚠️ <i>{error}</i>"])
    lines.extend(["", "<i>Balances are read live from Solana.</i>"])
    return "\n".join(lines)


async def _load_wallet_snapshot(connection, address: str, http_client=None, env: Env | None = None):
    sol_balance = await get_sol_balance(connection, address)
    owner = Pubkey.from_string(address)
    from solana.rpc.models import TokenAccountOpts
    token_program = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    response = await connection.get_token_accounts_by_owner_json_parsed(
        owner, TokenAccountOpts(program_id=token_program)
    )
    aggregate: dict[str, tuple[int, int]] = {}
    for account in response.value:
        try:
            info = account.account.data.parsed["info"]
            token_amount = info["tokenAmount"]
            raw = int(token_amount["amount"])
            decimals = int(token_amount["decimals"])
            mint = str(info["mint"])
            if raw > 0:
                previous = aggregate.get(mint, (0, decimals))
                aggregate[mint] = (previous[0] + raw, decimals)
        except Exception:  # noqa: BLE001 - skip malformed token account without hiding the wallet balance
            continue
    holdings = sorted(((mint, raw / (10 ** decimals)) for mint, (raw, decimals) in aggregate.items()), key=lambda item: item[1], reverse=True)
    return sol_balance, holdings


def register_wallet_commands(session_factory: async_sessionmaker[AsyncSession], env: Env, connection=None) -> Router:
    log.info("Wallet dashboard commands registered", wallet_command="/wallet", rpc_ready=connection is not None)

    @router.message(Command("wallet"))
    async def wallet_handler(message: Message) -> None:
        if message.from_user is None:
            return
        telegram_id = str(message.from_user.id)
        async with session_factory() as session:
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()
            address = user.wallet_public_key if user else None
        if not address:
            await message.answer("👛 No wallet connected yet. Use /connectwallet first.")
            return
        if connection is None:
            await message.answer("⚠️ Wallet data service is not ready yet. Please try again shortly.")
            return
        try:
            sol_balance, holdings = await _load_wallet_snapshot(connection, address)
            await message.answer(_wallet_card(address, sol_balance, None, holdings), parse_mode="HTML", reply_markup=_wallet_keyboard())
        except Exception as err:  # noqa: BLE001 - surface a safe UI error, never raw RPC details
            log.error("Wallet dashboard refresh failed", err=str(err), user_id=telegram_id)
            await message.answer("⚠️ Could not load the live wallet right now. Tap /wallet again in a moment.")

    @router.callback_query(F.data.startswith("wallet:"))
    async def wallet_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        action = (callback.data or "").split(":", 1)[-1]
        if action == "buy":
            await callback.answer()
            await callback.message.answer("💳 Manual buy: /buy <token_mint> <usd_amount> [slippage_bps]")
            return
        if action == "sell":
            await callback.answer()
            await callback.message.answer("💸 Manual sell: /sell <token_mint> <token_amount|all> [slippage_bps]")
            return
        if action == "portfolio":
            await callback.answer()
            await callback.message.answer("📊 Use /portfolio to view your recent confirmed trades.")
            return
        if action == "disconnect":
            await callback.answer("Use /disconnectwallet to confirm the disconnect flow.")
            return
        if action == "refresh":
            telegram_id = str(callback.from_user.id)
            async with session_factory() as session:
                result = await session.execute(select(User).where(User.telegram_id == telegram_id))
                user = result.scalar_one_or_none()
                address = user.wallet_public_key if user else None
            if not address or connection is None:
                await callback.answer("No connected wallet.", show_alert=True)
                return
            try:
                sol_balance, holdings = await _load_wallet_snapshot(connection, address)
                await callback.message.edit_text(_wallet_card(address, sol_balance, None, holdings), parse_mode="HTML", reply_markup=_wallet_keyboard())
                await callback.answer("Wallet refreshed")
            except Exception as err:  # noqa: BLE001
                log.error("Wallet dashboard callback refresh failed", err=str(err), user_id=telegram_id)
                await callback.answer("Refresh failed; try /wallet again.", show_alert=True)

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
        if message.from_user is None:
            return
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
            if db_user is None:
                await message.answer("❌ Something went wrong saving your wallet — please try /connectwallet again.")
                return
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
        if message.from_user is None:
            return
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
