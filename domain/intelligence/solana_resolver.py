import html
import logging

from config.settings import HELIUS_API_KEY, MULTI_RPC_METADATA_CACHE_TTL_SECONDS
from providers.rpc.helius_request_manager import helius_manager, PRIORITY_NORMAL
from providers.marketdata.dexscreener import get_token_card_info

logger = logging.getLogger("AlphaPulse.SolanaResolver")

SYSTEM_PROGRAM = "11111111111111111111111111111111"

TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
    "TokenzQdYdSoUXzdZmE8mtbP2K72Cz8DnQXAsW5tF",    # Token-2022
}


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else "N/A"


def _rpc_url() -> str:
    """
    Uses Helius if available.
    Falls back to public Solana RPC if no key exists.

    NOTE: this is a label only for standard RPC calls (getAccountInfo, etc)
    — MultiRPCManager selects/builds the real per-provider endpoint itself
    based on the configured Helius -> QuickNode -> Alchemy -> dRPC failover
    order, so those calls are NOT actually restricted to Helius even though
    this function's name suggests otherwise. The one exception is
    get_asset_metadata()'s "getAsset" DAS call below, which the manager
    keeps Helius-only regardless of this URL, since only Helius implements it.
    """
    if HELIUS_API_KEY:
        return f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

    return "https://api.mainnet-beta.solana.com"


async def _rpc_call(method: str, params, priority: int = PRIORITY_NORMAL, cache_ttl: float = 30.0):
    payload = {
        "jsonrpc": "2.0",
        "id": "alphapulse-resolver",
        "method": method,
        "params": params,
    }

    data = await helius_manager.request_json(
        "POST",
        _rpc_url(),
        json_body=payload,
        priority=priority,
        cache_key=f"resolver:{method}:{params}",
        cache_ttl=cache_ttl,
        timeout=10,
        context=f"resolver_rpc:{method}",
    )

    if data is None:
        return None

    if data.get("error"):
        logger.warning(f"RPC error for {method}: {data['error']}")
        return None

    return data


async def get_account_info(address: str) -> dict | None:
    """
    Get parsed Solana account info.
    """
    data = await _rpc_call(
        "getAccountInfo",
        [
            address,
            {
                "encoding": "jsonParsed"
            }
        ]
    )

    if not data:
        return None

    return data.get("result", {}).get("value")


async def get_asset_metadata(address: str) -> dict:
    """
    Get token/NFT metadata.

    Primary source: Helius DAS `getAsset` — richest data (name, symbol,
    interface, decimals, supply in one call). This is a Helius-proprietary
    API with no equivalent on Alchemy/dRPC/QuickNode's standard Solana
    RPC, so it can't get true multi-provider RPC failover the way
    getAccountInfo/getBalance/etc. do; MultiRPCManager still applies its
    circuit breaker/retry logic to it, it just only ever targets Helius.

    Fallback (Helius unavailable/not configured/failed): DexScreener's
    token-pair lookup for name/symbol only (decimals/supply for a mint
    already come from get_account_info()'s standard getAccountInfo call in
    resolve_solana_address() below, independent of this function, so those
    fields don't need a fallback here). This won't have a name/symbol for a
    token with no active DEX pair yet (e.g. still on a Pump.fun bonding
    curve) — that's an inherent limit of DexScreener as a data source, not
    a bug.

    Cached for MULTI_RPC_METADATA_CACHE_TTL_SECONDS (metadata is
    effectively static once a token is deployed).
    """
    if HELIUS_API_KEY:
        data = await _rpc_call(
            "getAsset",
            {"id": address},
            cache_ttl=MULTI_RPC_METADATA_CACHE_TTL_SECONDS,
        )

        if data:
            result = data.get("result") or {}
            content = result.get("content") or {}
            metadata = content.get("metadata") or {}
            token_info = result.get("token_info") or {}

            name = metadata.get("name") or token_info.get("name") or ""
            symbol = metadata.get("symbol") or token_info.get("symbol") or ""

            if name or symbol:
                return {
                    "interface": result.get("interface", ""),
                    "name": name,
                    "symbol": symbol,
                    "decimals": token_info.get("decimals"),
                    "supply": token_info.get("supply"),
                }
            # Helius answered but had no usable name/symbol (e.g. a mint it
            # simply hasn't indexed metadata for) — fall through to the
            # DexScreener fallback below rather than returning a blank name.

    cached_fallback = helius_manager.get_cached(f"resolver:dexscreener_metadata:{address}")
    if cached_fallback is not None:
        return cached_fallback

    card = await get_token_card_info(address)
    fallback = {
        "interface": "",
        "name": (card or {}).get("name") or "",
        "symbol": (card or {}).get("symbol") or "",
        "decimals": None,
        "supply": None,
    }
    helius_manager.set_cached(
        f"resolver:dexscreener_metadata:{address}", fallback, MULTI_RPC_METADATA_CACHE_TTL_SECONDS
    )
    return fallback


async def resolve_solana_address(address: str) -> dict:
    """
    Resolve what kind of Solana address this is.

    Returns:
    {
        "kind": "wallet" | "token_mint" | "token_account" | "program" | "account" | "unknown",
        ...
    }
    """
    account = await get_account_info(address)

    if not account:
        return {
            "address": address,
            "exists": False,
            "kind": "unknown",
            "message": "No Solana account found for this address.",
        }

    owner = account.get("owner", "")
    lamports = account.get("lamports", 0)
    executable = account.get("executable", False)

    parsed_type = ""
    parsed_info = {}

    data = account.get("data")

    if isinstance(data, dict):
        parsed = data.get("parsed") or {}
        parsed_type = parsed.get("type", "")
        parsed_info = parsed.get("info") or {}

    kind = "account"

    if owner == SYSTEM_PROGRAM:
        kind = "wallet"
    elif parsed_type == "mint":
        kind = "token_mint"
    elif parsed_type == "account":
        kind = "token_account"
    elif executable:
        kind = "program"
    elif owner in TOKEN_PROGRAMS:
        kind = "token_program_account"

    metadata = {}

    if kind in {"token_mint", "unknown", "account", "token_program_account"}:
        metadata = await get_asset_metadata(address)

    result = {
        "address": address,
        "exists": True,
        "kind": kind,
        "owner": owner,
        "lamports": lamports,
        "parsed_type": parsed_type,
        "parsed_info": parsed_info,
        "metadata": metadata,
    }

    if kind == "token_mint":
        result["decimals"] = parsed_info.get("decimals")
        result["supply"] = parsed_info.get("supply")

    if kind == "token_account":
        result["mint"] = parsed_info.get("mint")
        result["token_owner"] = parsed_info.get("owner")
        result["token_amount"] = parsed_info.get("tokenAmount")

    return result


def format_resolution_message(address: str, resolved: dict) -> str:
    """
    Format a helpful Telegram message when DexScreener has no pair.
    """
    kind = resolved.get("kind", "unknown")
    metadata = resolved.get("metadata") or {}

    solscan_account = f"https://solscan.io/account/{address}"
    solscan_token = f"https://solscan.io/token/{address}"
    pumpfun = f"https://pump.fun/coin/{address}"
    dexscreener_search = f"https://dexscreener.com/solana/{address}"

    if kind == "token_mint":
        name = metadata.get("name") or "Unknown Token"
        symbol = metadata.get("symbol") or "???"

        decimals = resolved.get("decimals", "N/A")
        supply = resolved.get("supply", "N/A")

        return (
            "🪙 <b>Token Mint Detected</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📛 Name: <b>{_esc(name)}</b>\n"
            f"🔤 Symbol: <b>{_esc(symbol)}</b>\n"
            f"🧮 Decimals: <b>{_esc(decimals)}</b>\n"
            f"📦 Supply: <b>{_esc(supply)}</b>\n\n"
            "⚠️ <b>No active DexScreener pair found.</b>\n\n"
            "Possible reasons:\n"
            "• Token is too new\n"
            "• No DEX liquidity yet\n"
            "• Still on Pump.fun / bonding curve\n"
            "• Not indexed by DexScreener yet\n\n"
            f"🔎 <a href=\"{solscan_token}\">Solscan</a> | "
            f"<a href=\"{pumpfun}\">Pump.fun</a> | "
            f"<a href=\"{dexscreener_search}\">DexScreener</a>\n\n"
            f"<code>{address}</code>"
        )

    if kind == "wallet":
        sol_balance = 0

        try:
            sol_balance = float(resolved.get("lamports", 0)) / 1_000_000_000
        except (ValueError, TypeError):
            sol_balance = 0

        return (
            "👛 <b>Wallet Address Detected</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 SOL Balance: <b>{sol_balance:.4f} SOL</b>\n\n"
            "AlphaPulse did not find fungible token holdings for this wallet.\n\n"
            "Try:\n"
            f"<code>/activity {address}</code>\n\n"
            f"🔎 <a href=\"{solscan_account}\">View on Solscan</a>\n\n"
            f"<code>{address}</code>"
        )

    if kind == "token_account":
        mint = resolved.get("mint", "")

        return (
            "📦 <b>Token Account Detected</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "This address is a token account, not the token mint/contract.\n\n"
            f"🪙 Token Mint:\n<code>{_esc(mint)}</code>\n\n"
            "Try pasting the token mint above instead.\n\n"
            f"🔎 <a href=\"{solscan_account}\">View account on Solscan</a>"
        )

    if kind == "program":
        return (
            "🧩 <b>Program Address Detected</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "This looks like a Solana program address, not a token contract or wallet.\n\n"
            f"🔎 <a href=\"{solscan_account}\">View on Solscan</a>\n\n"
            f"<code>{address}</code>"
        )

    if kind == "unknown" or not resolved.get("exists"):
        return (
            "❓ <b>Unknown Solana Address</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "AlphaPulse could not find an active Solana account or DEX pair for this address.\n\n"
            "Possible reasons:\n"
            "• Invalid address\n"
            "• Account not initialized\n"
            "• Token not created yet\n"
            "• RPC/indexer delay\n\n"
            f"<code>{address}</code>"
        )

    return (
        "ℹ️ <b>Solana Account Detected</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Type: <b>{_esc(kind)}</b>\n"
        f"Owner Program:\n<code>{_esc(resolved.get('owner'))}</code>\n\n"
        f"🔎 <a href=\"{solscan_account}\">View on Solscan</a>\n\n"
        f"<code>{address}</code>"
    )
