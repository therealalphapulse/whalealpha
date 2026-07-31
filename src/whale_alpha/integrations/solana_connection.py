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

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.pubkey import Pubkey

from whale_alpha.config import Env


def create_connection(env: Env) -> AsyncClient:
    return AsyncClient(env.SOLANA_RPC_URL, commitment=Confirmed)


def is_valid_solana_address(address: str) -> bool:
    try:
        Pubkey.from_string(address)
        return True
    except Exception:
        return False


async def get_sol_balance(connection: AsyncClient, address: str) -> float:
    pubkey = Pubkey.from_string(address)
    resp = await connection.get_balance(pubkey)
    lamports = resp.value
    return lamports / 1e9
