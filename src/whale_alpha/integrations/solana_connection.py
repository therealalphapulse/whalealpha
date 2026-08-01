"""Solana RPC connection helpers — port of src/integrations/solana/connection.ts.

TODO(integration), carried over verbatim from the original: wallet monitoring
at scale should not poll getBalance per wallet. For 500-1500 tracked wallets,
subscribe to program account changes / use an indexer (Helius webhooks,
Triton, or your own geyser plugin) and push events into engines/monitor rather
than polling RPC directly. This module intentionally exposes only thin,
correct primitives — wire your indexer's event stream to
engines/monitor.ingest_wallet_buy_event.
"""

from __future__ import annotations

import contextlib

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.models import TokenAccountOpts
from solders.pubkey import Pubkey

from whale_alpha.config import Env


def create_connection(env: Env) -> AsyncClient:
    return AsyncClient(env.SOLANA_RPC_URL, commitment=Confirmed)


def is_valid_solana_address(address: str) -> bool:
    try:
        Pubkey.from_string(address)
        return True
    except Exception:  # noqa: BLE001 — any parse failure means "not a valid address"
        return False


async def get_sol_balance(connection: AsyncClient, address: str) -> float:
    pubkey = Pubkey.from_string(address)
    resp = await connection.get_balance(pubkey)
    lamports = resp.value
    return lamports / 1e9


async def get_token_decimals(connection: AsyncClient, mint: str) -> int:
    """Fetches an SPL token mint's decimal precision via getTokenSupply —
    needed to convert a human token amount (e.g. "sell 1500 tokens") into the
    base units Jupiter's quote API expects.
    """
    resp = await connection.get_token_supply(Pubkey.from_string(mint))
    return resp.value.decimals


async def get_token_balance(connection: AsyncClient, owner_address: str, mint: str) -> tuple[int, int]:
    """Returns (raw_base_units, decimals) of `owner_address`'s balance of `mint`,
    summed across every token account they hold for that mint (normally just
    one, but nothing prevents more). Returns (0, decimals) if they hold none.

    NOTE: uses jsonParsed encoding for convenience; if you're on an RPC
    provider that doesn't support jsonParsed for this call, decode the raw
    base64 SPL-token account layout instead.
    """
    owner = Pubkey.from_string(owner_address)
    mint_pubkey = Pubkey.from_string(mint)
    resp = await connection.get_token_accounts_by_owner_json_parsed(
        owner, TokenAccountOpts(mint=mint_pubkey)
    )

    total_raw = 0
    decimals = 0
    for account in resp.value:
        try:
            parsed = account.account.data.parsed  # type: ignore[union-attr]
            info = parsed["info"]["tokenAmount"]
            total_raw += int(info["amount"])
            decimals = int(info["decimals"])
        except Exception:  # noqa: BLE001 — skip a malformed account entry, don't fail the whole balance check
            continue

    if decimals == 0 and total_raw == 0:
        # No accounts found (or all failed to parse) — fall back to the
        # mint's own decimals so callers can still display "0" correctly.
        with contextlib.suppress(Exception):  # noqa: BLE001 — best-effort fallback
            decimals = await get_token_decimals(connection, mint)

    return total_raw, decimals
