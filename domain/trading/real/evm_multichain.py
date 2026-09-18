"""Multi-chain EVM balance + USD aggregation for the /wallet card.

Ethereum mainnet and Robinhood Chain are both plain EVM chains, so the
single address already stored on RealWallet (see models/real_wallet.py)
is valid on both -- there is nothing chain-specific about the keypair,
only about which RPC endpoint and native-asset price get queried. Each
chain's balance is fetched independently (one RPC failing/timing out
never blocks or fails the other), and a single ETH/USD price -- shared
by both chains, since both are ETH-denominated -- is used to compute
each chain's USD value and the combined total.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

from config.settings import ETHEREUM_RPC_URL, ROBINHOOD_RPC_URL
from domain.trading.real.robinhood_swap import eth_usd_price

logger = logging.getLogger("WhaleAlpha.EvmMultichain")

_RPC_TIMEOUT = aiohttp.ClientTimeout(total=10)


@dataclass
class ChainBalance:
    chain: str  # "ethereum" | "robinhood" -- stable key, not for display
    label: str  # display name, e.g. "Ethereum"
    native_balance: float | None  # in ETH units; None on fetch failure
    usd_value: float | None  # None if balance or price is unavailable


async def _rpc_native_balance(rpc_url: str, address: str, chain_label: str) -> float | None:
    """eth_getBalance against a single EVM RPC endpoint. Returns None
    (never raises) so a failure on one chain can't take the other down
    with it -- callers gather() both chains concurrently."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [address, "latest"]}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc_url, json=payload, timeout=_RPC_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.error(f"{chain_label} balance RPC HTTP {resp.status} for {address} via {rpc_url}")
                    return None
                data = await resp.json(content_type=None)
                if data.get("error") is not None:
                    logger.error(f"{chain_label} balance RPC error for {address}: {data['error']}")
                    return None
                return int(data["result"], 16) / 10**18
    except Exception as e:
        logger.error(f"{chain_label} balance fetch failed for {address} via {rpc_url}: {e}")
        return None


async def get_ethereum_balance(address: str) -> float | None:
    return await _rpc_native_balance(ETHEREUM_RPC_URL, address, "Ethereum")


async def get_robinhood_chain_balance(address: str) -> float | None:
    return await _rpc_native_balance(ROBINHOOD_RPC_URL, address, "Robinhood Chain")


async def get_multi_chain_snapshot(address: str) -> dict:
    """Fetches Ethereum + Robinhood Chain native balances independently,
    plus a shared ETH/USD price, and returns per-chain + combined totals
    for the /wallet card. Never raises -- any individual failure just
    shows as "--" for that chain / leaves it out of the combined total."""
    eth_balance, rh_balance, usd_price = await asyncio.gather(
        get_ethereum_balance(address),
        get_robinhood_chain_balance(address),
        eth_usd_price(),
    )

    def usd(balance: float | None) -> float | None:
        if balance is None or usd_price is None:
            return None
        return balance * usd_price

    chains = [
        ChainBalance("ethereum", "Ethereum", eth_balance, usd(eth_balance)),
        ChainBalance("robinhood", "Robinhood", rh_balance, usd(rh_balance)),
    ]

    known_usd_values = [c.usd_value for c in chains if c.usd_value is not None]
    total_usd = sum(known_usd_values) if known_usd_values else None

    return {"address": address, "chains": chains, "total_usd": total_usd}
