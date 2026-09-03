"""
Provider-agnostic wallet fungible-token holdings fetch.

Replaces the old Helius DAS `getAssetsByOwner` call (services/wallet_
portfolio.py and services/wallet_intelligence.py used to hit this directly,
hard-locking wallet balance/portfolio lookups to Helius) with two standard
Solana JSON-RPC methods that every configured provider supports:

  * getBalance              — native SOL balance
  * getTokenAccountsByOwner — every SPL token account owned by the wallet

Both go through the shared MultiRPCManager (services/multi_rpc_manager.py),
so they get full Helius -> QuickNode -> Alchemy -> dRPC failover, the shared
cache, dedup, and rate limiting exactly like every other RPC call in the bot.

Note: unlike DAS getAssetsByOwner, standard RPC does not return token name/
symbol/price metadata — callers already have (and keep) their own fallback
naming (e.g. wallet_intelligence.extract_token_name_symbol degrades to
"Token <mint prefix>" / "UNKNOWN") and downstream DexScreener enrichment for
live price/name/symbol, so nothing here needs to change to accommodate that.
"""

import logging

from providers.rpc.helius_request_manager import helius_manager, PRIORITY_LOW

logger = logging.getLogger("AlphaPulse.SolanaTokenHoldings")

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


async def fetch_wallet_holdings(
    wallet_address: str,
    priority: int = PRIORITY_LOW,
    cache_key_prefix: str = "wallet_holdings",
    cache_ttl: float | None = None,
) -> dict | None:
    """
    Fetch a wallet's native SOL balance + SPL token holdings via standard
    Solana JSON-RPC (full multi-provider failover).

    Returns:
      {"native_sol": float, "tokens": [{"mint": str, "amount": float,
       "decimals": int}, ...]}
      on success (amount already normalized by decimals, i.e. a UI amount,
      matching what the old DAS-based code produced).

      None if either RPC call fails outright on every configured provider —
      callers must treat this as "unknown", not zero (same contract the
      DAS-based fetchers used).
    """
    balance_cache_key = f"{cache_key_prefix}:balance:{wallet_address}"
    tokens_cache_key = f"{cache_key_prefix}:tokens:{wallet_address}"

    cached_balance = helius_manager.get_cached(balance_cache_key) if cache_ttl else None
    cached_tokens = helius_manager.get_cached(tokens_cache_key) if cache_ttl else None

    if cached_balance is not None and cached_tokens is not None:
        return {"native_sol": cached_balance, "tokens": cached_tokens}

    balance_payload = {
        "jsonrpc": "2.0",
        "id": "alphapulse-wallet-balance",
        "method": "getBalance",
        "params": [wallet_address],
    }
    tokens_payload = {
        "jsonrpc": "2.0",
        "id": "alphapulse-wallet-tokens",
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet_address,
            {"programId": TOKEN_PROGRAM_ID},
            {"encoding": "jsonParsed"},
        ],
    }

    balance_data = await helius_manager.request_json(
        "POST",
        "solana-json-rpc:getBalance",
        json_body=balance_payload,
        priority=priority,
        timeout=10,
        context=f"wallet_balance:{wallet_address}",
    )

    if balance_data is None or balance_data.get("error"):
        if balance_data is not None:
            logger.warning(f"getBalance error for {wallet_address}: {balance_data.get('error')}")
        return None

    lamports = ((balance_data.get("result") or {}).get("value")) or 0
    native_sol = _to_float(lamports) / 1_000_000_000

    tokens_data = await helius_manager.request_json(
        "POST",
        "solana-json-rpc:getTokenAccountsByOwner",
        json_body=tokens_payload,
        priority=priority,
        timeout=15,
        context=f"wallet_tokens:{wallet_address}",
    )

    if tokens_data is None or tokens_data.get("error"):
        if tokens_data is not None:
            logger.warning(f"getTokenAccountsByOwner error for {wallet_address}: {tokens_data.get('error')}")
        return None

    tokens: list[dict] = []
    for entry in (tokens_data.get("result") or {}).get("value") or []:
        if not isinstance(entry, dict):
            continue
        account = entry.get("account") or {}
        parsed_data = account.get("data")
        parsed_info = (
            (parsed_data.get("parsed") or {}).get("info")
            if isinstance(parsed_data, dict) else None
        )
        if not parsed_info:
            continue

        mint = parsed_info.get("mint")
        token_amount = parsed_info.get("tokenAmount") or {}
        if not mint:
            continue

        amount = token_amount.get("uiAmount")
        if amount is None:
            amount = _to_float(token_amount.get("amount")) / (10 ** int(token_amount.get("decimals", 0) or 0))
        else:
            amount = _to_float(amount)

        if amount <= 0:
            continue

        tokens.append({
            "mint": mint,
            "amount": amount,
            "decimals": token_amount.get("decimals", 0),
        })

    if cache_ttl:
        helius_manager.set_cached(balance_cache_key, native_sol, cache_ttl)
        helius_manager.set_cached(tokens_cache_key, tokens, cache_ttl)

    return {"native_sol": native_sol, "tokens": tokens}
