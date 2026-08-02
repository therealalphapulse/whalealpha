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


async def get_token_largest_accounts(connection: AsyncClient, mint: str, limit: int = 20) -> list[str]:
    """Returns up to `limit` owner addresses holding the largest balances of
    `mint`, via plain RPC `getTokenLargestAccounts` (no indexer needed).

    Used as the discovery engine's always-available candidate source (see
    integrations/wallet_discovery_source.py): tokens that already produced a
    real Signal are, by definition, tokens multiple tracked whales bought —
    so their other large holders are a reasonable pool of untracked wallets
    worth evaluating. `getTokenLargestAccounts` returns *token accounts*, not
    owners directly, so each is resolved via `getAccountInfo` (jsonParsed) to
    its owning wallet. Note this call is capped at 20 by the RPC spec itself;
    `limit` only trims further, it cannot request more than the RPC returns.
    """
    resp = await connection.get_token_largest_accounts(Pubkey.from_string(mint))
    token_accounts = [entry.address for entry in resp.value][:limit]

    owners: list[str] = []
    for token_account in token_accounts:
        try:
            info = await connection.get_account_info_json_parsed(token_account)
            parsed = info.value.data.parsed  # type: ignore[union-attr]
            owner = parsed["info"]["owner"]
            if owner:
                owners.append(owner)
        except Exception:  # noqa: BLE001 — skip an unparseable/closed account, don't fail the batch
            continue
    return owners


async def get_wallet_first_activity_slot(connection: AsyncClient, address: str) -> int | None:
    """Best-effort wallet age proxy: the slot of the oldest transaction signature
    RPC will still return for this address. Solana RPC nodes only retain a
    limited signature history (varies by provider), so for very old wallets
    this under-counts age rather than over-counts it — acceptable for a
    "is this wallet at least N days old" gate, not exact enough to display as
    a precise age. Returns None if the address has no history at all.
    """
    pubkey = Pubkey.from_string(address)
    oldest_signature = None
    before = None
    # Page backwards through signature history to the oldest page RPC will
    # give us — capped at a few pages so one candidate can't blow the
    # discovery cycle's time/RPC budget.
    for _ in range(5):
        resp = await connection.get_signatures_for_address(pubkey, before=before, limit=1000)
        if not resp.value:
            break
        oldest_signature = resp.value[-1]
        if len(resp.value) < 1000:
            break
        before = oldest_signature.signature

    if oldest_signature is None:
        return None
    return oldest_signature.slot


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
