import logging

from config.settings import (
    HELIUS_API_KEY,
    HELIUS_API,
    HELIUS_WALLET_CACHE_TTL_SECONDS,
)
from providers.rpc.helius_request_manager import (
    helius_manager,
    PRIORITY_LOW,
)

logger = logging.getLogger("AlphaPulse.Helius")


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _same_address(a: str, b: str) -> bool:
    return (a or "").lower() == (b or "").lower()


def _rpc_url() -> str:
    """
    Prefer Helius RPC if key exists.
    Fall back to public Solana RPC if not.
    """
    if HELIUS_API_KEY:
        return f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

    return "https://api.mainnet-beta.solana.com"


async def _rpc_call(method: str, params, priority: int = PRIORITY_LOW):
    payload = {
        "jsonrpc": "2.0",
        "id": "alphapulse-activity",
        "method": method,
        "params": params,
    }

    data = await helius_manager.request_json(
        "POST",
        _rpc_url(),
        json_body=payload,
        priority=priority,
        timeout=15,
        context=f"rpc:{method}",
    )

    if data is None:
        return None

    if data.get("error"):
        logger.warning(f"RPC error for {method}: {data['error']}")
        return None

    return data.get("result")


async def get_recent_signatures(wallet_address: str, limit: int = 5) -> list[dict]:
    """
    Fallback method using Solana RPC getSignaturesForAddress.
    This does not decode swaps/transfers, but proves recent activity exists.
    """

    safe_limit = max(1, min(limit, 20))

    result = await _rpc_call(
        "getSignaturesForAddress",
        [
            wallet_address,
            {
                "limit": safe_limit
            }
        ]
    )

    if not isinstance(result, list):
        return []

    events = []

    for item in result:
        if not isinstance(item, dict):
            continue

        signature = item.get("signature", "")
        block_time = item.get("blockTime", 0)
        err = item.get("err")

        status = "Failed" if err else "Success"

        events.append({
            "type": "TX",
            "token": "SOLANA_TX",
            "amount": 0,
            "from": "",
            "to": "",
            "timestamp": block_time,
            "signature": signature,
            "description": f"Recent Solana transaction • {status}",
            "raw_type": "RPC_SIGNATURE",
        })

    logger.info(
        f"RPC fallback found {len(events)} signatures for {wallet_address}"
    )

    return events


async def get_wallet_transactions(
    wallet_address: str, limit: int = 5, priority: int = PRIORITY_LOW
) -> list[dict]:
    """
    Fetch recent transactions for a Solana wallet.

    Primary:
    - Helius Enhanced Transactions API

    Fallback:
    - Solana RPC getSignaturesForAddress

    `priority` defaults to background priority (used by signal scanning /
    whale tracking / smart wallet intelligence); pass PRIORITY_HIGH for
    user-triggered lookups so they aren't queued behind background work.
    """

    if not HELIUS_API_KEY:
        logger.warning("HELIUS_API_KEY not set. Trying RPC fallback only.")
        return await get_recent_signatures(wallet_address, limit=limit)

    wallet_address = wallet_address.strip().strip(",.;")
    safe_limit = max(1, min(limit, 20))

    url = f"{HELIUS_API}/addresses/{wallet_address}/transactions"

    params = {
        "api-key": HELIUS_API_KEY,
        "limit": safe_limit,
    }

    payload = await helius_manager.request_json(
        "GET",
        url,
        params=params,
        priority=priority,
        cache_key=f"wallet_tx:{wallet_address}:{safe_limit}",
        cache_ttl=HELIUS_WALLET_CACHE_TTL_SECONDS,
        timeout=15,
        context=f"wallet_transactions:{wallet_address}",
    )

    if payload is None:
        # Fallback if enhanced endpoint fails/rate-limits/times out.
        return await get_recent_signatures(wallet_address, limit=safe_limit)

    try:
        if isinstance(payload, list):
            transactions = payload
        elif isinstance(payload, dict):
            transactions = (
                payload.get("data")
                or payload.get("transactions")
                or payload.get("result")
                or []
            )
        else:
            transactions = []

        if not transactions:
            logger.info(
                f"Helius enhanced returned no transactions for {wallet_address}. Trying RPC fallback."
            )
            return await get_recent_signatures(wallet_address, limit=safe_limit)

        results = []

        for tx in transactions:
            if not isinstance(tx, dict):
                continue

            signature = tx.get("signature", "")
            timestamp = tx.get("timestamp", 0)
            tx_type = tx.get("type", "TRANSACTION")
            description = tx.get("description", "")

            added_transfer = False

            token_transfers = tx.get("tokenTransfers") or []

            for transfer in token_transfers:
                if not isinstance(transfer, dict):
                    continue

                from_user = (
                    transfer.get("fromUserAccount")
                    or transfer.get("fromUserAccountOwner")
                    or ""
                )

                to_user = (
                    transfer.get("toUserAccount")
                    or transfer.get("toUserAccountOwner")
                    or ""
                )

                mint = (
                    transfer.get("mint")
                    or transfer.get("tokenMint")
                    or transfer.get("tokenAddress")
                    or "UNKNOWN_TOKEN"
                )

                amount = _to_float(
                    transfer.get("tokenAmount")
                    or transfer.get("amount")
                    or 0
                )

                if _same_address(from_user, wallet_address):
                    direction = "OUT"
                elif _same_address(to_user, wallet_address):
                    direction = "IN"
                else:
                    continue

                results.append({
                    "type": direction,
                    "token": mint,
                    "amount": amount,
                    "from": from_user,
                    "to": to_user,
                    "timestamp": timestamp,
                    "signature": signature,
                    "description": description or f"{direction} token transfer",
                    "raw_type": tx_type,
                })

                added_transfer = True

            native_transfers = tx.get("nativeTransfers") or []

            for transfer in native_transfers:
                if not isinstance(transfer, dict):
                    continue

                from_user = transfer.get("fromUserAccount", "")
                to_user = transfer.get("toUserAccount", "")

                amount_lamports = _to_float(transfer.get("amount", 0))
                amount_sol = amount_lamports / 1_000_000_000

                if _same_address(from_user, wallet_address):
                    direction = "OUT"
                elif _same_address(to_user, wallet_address):
                    direction = "IN"
                else:
                    continue

                results.append({
                    "type": direction,
                    "token": "SOL",
                    "amount": amount_sol,
                    "from": from_user,
                    "to": to_user,
                    "timestamp": timestamp,
                    "signature": signature,
                    "description": description or f"{direction} SOL transfer",
                    "raw_type": tx_type,
                })

                added_transfer = True

            # If enhanced tx exists but no direct transfer matched,
            # still show transaction activity.
            if not added_transfer:
                results.append({
                    "type": "TX",
                    "token": tx_type,
                    "amount": 0,
                    "from": "",
                    "to": "",
                    "timestamp": timestamp,
                    "signature": signature,
                    "description": description or "Recent wallet transaction",
                    "raw_type": tx_type,
                })

        if not results:
            logger.info(
                f"Enhanced tx existed but no parsed events for {wallet_address}. Trying RPC fallback."
            )
            return await get_recent_signatures(wallet_address, limit=safe_limit)

        logger.info(
            f"Parsed {len(results)} activity events from {len(transactions)} enhanced transactions for {wallet_address}"
        )

        return results[:safe_limit]

    except Exception as e:
        logger.error(f"Helius enhanced error for {wallet_address}: {e}. Trying RPC fallback.")
        return await get_recent_signatures(wallet_address, limit=safe_limit)


async def get_token_holders(
    contract_address: str, limit: int = 10, priority: int = PRIORITY_LOW
) -> list[dict]:
    """
    Fetch token metadata from Helius.

    Note:
    This is not exact top holder analysis. Exact holder count is handled separately.
    """

    if not HELIUS_API_KEY:
        return []

    url = f"{HELIUS_API}/token-metadata"
    params = {"api-key": HELIUS_API_KEY}
    body = {"mintAccounts": [contract_address]}

    data = await helius_manager.request_json(
        "POST",
        url,
        params=params,
        json_body=body,
        priority=priority,
        cache_key=f"token_metadata:{contract_address}",
        cache_ttl=HELIUS_WALLET_CACHE_TTL_SECONDS,
        timeout=10,
        context=f"token_metadata:{contract_address}",
    )

    return data if isinstance(data, list) else []
