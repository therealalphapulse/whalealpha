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


def register_scanner_commands(env: Env, http_client: httpx.AsyncClient) -> Router:
    @router.message(Command("scan"))
    async def scan_handler(message: Message) -> None:
        args = (message.text or "").split()[1:]
        if len(args) != 1:
            await message.answer(
                "🔎 <b>Whale Alpha Token Scanner</b>\n\n"
                "Scan any Solana meme token by mint address.\n\n"
                "<b>Usage</b>\n"
                "<code>/scan TOKEN_MINT</code>\n\n"
                "Example:\n<code>/scan 7xKXtg2CW87d97TXJSDpbD5jBkhe7h7G6KqX...</code>",
                parse_mode="HTML",
            )
            return

        mint = args[0].strip()
        if not is_valid_solana_address(mint):
            await message.answer("❌ That is not a valid Solana token mint address.")
            return

        status = await message.answer("🔎 <b>Scanning token…</b>\nFetching live market data.", parse_mode="HTML")
        try:
            snapshot = await enrich_token(http_client, env, mint)
        except (httpx.HTTPError, ValueError, RuntimeError) as err:
            snapshot = None
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

    return router
