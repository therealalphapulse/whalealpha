"""User-facing /scan command for any Solana token mint."""

from __future__ import annotations

import httpx
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from whale_alpha.config import Env
from whale_alpha.integrations.solana_connection import is_valid_solana_address
from whale_alpha.integrations.token_hunter_market import enrich_token
from whale_alpha.services.token_scanner import build_scan_card

router = Router(name="scanner")


def is_plain_contract_address(text: str | None) -> bool:
    if not text:
        return False
    value = text.strip()
    return bool(value) and not value.startswith("/") and is_valid_solana_address(value)


def register_scanner_commands(env: Env, http_client: httpx.AsyncClient) -> Router:
    async def _scan_token(message: Message, mint: str) -> None:
        mint = mint.strip()
        if not is_valid_solana_address(mint):
            await message.answer("❌ That is not a valid Solana token mint address.")
            return

        status = await message.answer("🔎 <b>Scanning token…</b>\nFetching live market data.", parse_mode="HTML")
        try:
            snapshot = await enrich_token(http_client, env, mint)
        except (httpx.HTTPError, ValueError, RuntimeError) as err:
            await status.edit_text(
                "❌ <b>Token scan unavailable</b>\n\n"
                "The market-data provider returned an error. Please retry shortly.\n\n"
                f"<code>{type(err).__name__}</code>",
                parse_mode="HTML",
            )
            return

        if snapshot is None:
            await status.edit_text(
                "❌ <b>Token scan unavailable</b>\n\n"
                "No live Solana market pair was returned for this mint. "
                "The token may be too new, unlisted, inactive, or the provider may be temporarily unavailable.\n\n"
                f"Mint: <code>{mint}</code>",
                parse_mode="HTML",
            )
            return

        await status.edit_text(build_scan_card(snapshot), parse_mode="HTML")

    @router.message(Command("scan"))
    async def scan_handler(message: Message) -> None:
        args = (message.text or "").split()[1:]
        if len(args) != 1:
            await message.answer(
                "🔎 <b>Whale Alpha Token Scanner</b>\n\n"
                "Send only the Solana token contract address to scan it instantly.\n\n"
                "<b>Optional</b>\n<code>/scan TOKEN_MINT</code>",
                parse_mode="HTML",
            )
            return
        await _scan_token(message, args[0])

    @router.message(lambda message: is_plain_contract_address(message.text))
    async def contract_address_handler(message: Message) -> None:
        await _scan_token(message, message.text.strip())

    return router
