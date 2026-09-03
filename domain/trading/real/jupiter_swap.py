"""
Real, on-chain swap execution via the Jupiter Aggregator API.

Flow for every trade:
  1. get_quote()            - ask Jupiter for the best route/price
  2. build_swap_transaction() - ask Jupiter to build the (unsigned) transaction
  3. sign_send_and_confirm() - sign locally, preflight-simulate, submit, confirm

The private key is decrypted just before step 3 and only lives in local variables
for the duration of that call — see real_trade_engine.py.
"""

import asyncio
import base64
import logging
import time

import aiohttp
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from config.settings import (
    HELIUS_API_KEY,
    JUPITER_API_KEY,
    QUICKNODE_SOLANA_RPC,
    QUICKNODE_API_KEY,
)

logger = logging.getLogger("AlphaPulse.JupiterSwap")

REAL_WALLET_EXCLUDED_PROVIDERS = ["alchemy", "drpc"]

if JUPITER_API_KEY:
    _JUPITER_BASE = "https://api.jup.ag/swap/v1"
else:
    _JUPITER_BASE = "https://lite-api.jup.ag/swap/v1"

JUPITER_QUOTE_API = f"{_JUPITER_BASE}/quote"
JUPITER_SWAP_API = f"{_JUPITER_BASE}/swap"


def _jupiter_headers() -> dict:
    return {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}


WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
CONFIRMATION_TIMEOUT_S = 45
# Solana confirms new blocks roughly every 400-800ms; polling every 2s added
# up to ~1.5s of pure observation lag on top of actual confirmation time
# before the app noticed a trade had landed. The confirmation logic, timeout
# ceiling, and failure handling below are unchanged -- only how often we ask.
CONFIRMATION_POLL_INTERVAL_S = 0.5
_MINT_DECIMALS_CACHE_TTL_SECONDS = 6 * 60 * 60
_MINT_DECIMALS_CACHE: dict[str, tuple[int, float]] = {}
_RPC_BROADCAST_TIMEOUT_S = 30
_RPC_BROADCAST_ATTEMPTS_PER_PROVIDER = 2
_MAX_RPC_ERROR_LOG_CHARS = 600

# Fast-path reads (balance / decimals / confirmation polling) don't need
# the 30s broadcast timeout — a provider that hasn't answered a simple
# getBalance/getTokenAccountsByOwner/getSignatureStatuses call in a few
# seconds is not "about to succeed", it's the failover signal to move on.
_RPC_FAST_READ_TIMEOUT_S = 8
_RPC_FAST_READ_ATTEMPTS_PER_PROVIDER = 1

if HELIUS_API_KEY:
    SOLANA_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
else:
    SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"


def _real_wallet_rpc_endpoints() -> list[tuple[str, str]]:
    endpoints: list[tuple[str, str]] = []
    if HELIUS_API_KEY:
        endpoints.append(("helius", SOLANA_RPC_URL))
    if QUICKNODE_SOLANA_RPC:
        endpoints.append(("quicknode", QUICKNODE_SOLANA_RPC))
    elif QUICKNODE_API_KEY:
        endpoints.append(("quicknode", f"https://solana-mainnet.rpc.quicknode.io/{QUICKNODE_API_KEY}"))
    return endpoints


def _sanitize_rpc_error(error: object) -> str:
    text = str(error).replace("\n", " ").replace("\r", " ")
    for _, endpoint in _real_wallet_rpc_endpoints():
        if endpoint and endpoint in text:
            text = text.replace(endpoint, "<rpc-endpoint>")
    return text[:_MAX_RPC_ERROR_LOG_CHARS]


class SwapError(RuntimeError):
    pass


async def realwallet_rpc_call(
    payload: dict,
    *,
    context: str = "",
    timeout_s: float = _RPC_FAST_READ_TIMEOUT_S,
    attempts_per_provider: int = _RPC_FAST_READ_ATTEMPTS_PER_PROVIDER,
) -> dict:
    """
    Direct, bounded, multi-provider JSON-RPC call for every RealWallet-
    critical operation: balance/decimals reads, blockhash/account-exists
    checks, and (via the higher timeout/attempts used by
    _broadcast_signed_transaction, which predates and inspired this)
    transaction broadcast.

    Deliberately bypasses providers.rpc.multi_rpc_manager's shared queue.
    That queue is also where discovery/scoring/holder-scanning background
    traffic goes, so a burst of that LOW-priority load can rate-limit,
    circuit-break, or simply run the exponential-backoff retry envelope
    (multiple full provider rotations, each capped at 30s of backoff) on
    a provider a live wallet call also needed — turning a one-off hiccup
    into a multi-minute stall or an outright "did not complete after
    retries" failure. A live BUY/SELL/TP-SL/WITHDRAW must never wait on,
    or be starved by, unrelated traffic.

    Tries every configured RealWallet execution endpoint
    (_real_wallet_rpc_endpoints(): Helius first, then QuickNode — the
    same providers already used for transaction broadcast/simulation) in
    priority order, with a short per-attempt timeout. A failure (timeout,
    network error, HTTP error, JSON-RPC error) moves to the next
    provider immediately, with no inter-provider delay, so the primary
    failing over to the fallback is fast by construction. Only once
    every provider/attempt combination has been exhausted does this
    raise — so a single provider's outage, rate limit, or slow response
    can never sink the request, and this can never fabricate or guess a
    balance: either a provider returns a clean, verified on-chain
    response, or the call raises and the caller treats the amount as
    unknown (never as zero).

    Raises SwapError (never returns None) if no provider produced a
    clean, error-free JSON-RPC response.
    """
    endpoints = _real_wallet_rpc_endpoints()
    if not endpoints:
        raise SwapError("RealWallet RPC is not configured: no Helius or QuickNode execution endpoint is available.")

    provider_errors: list[str] = []
    async with aiohttp.ClientSession() as session:
        for provider_name, endpoint in endpoints:
            for _attempt in range(attempts_per_provider):
                try:
                    async with session.post(
                        endpoint, json=payload,
                        timeout=aiohttp.ClientTimeout(total=timeout_s),
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            provider_errors.append(f"{provider_name}=HTTP {resp.status}: {_sanitize_rpc_error(body[:200])}")
                            continue
                        try:
                            data = await resp.json(content_type=None)
                        except Exception as exc:
                            provider_errors.append(f"{provider_name}=invalid JSON response: {_sanitize_rpc_error(exc)}")
                            continue
                        if isinstance(data, dict) and data.get("error") is not None:
                            provider_errors.append(f"{provider_name}=RPC error {_sanitize_rpc_error(data['error'])}")
                            continue
                        return data
                except asyncio.TimeoutError:
                    provider_errors.append(f"{provider_name}=timeout after {timeout_s}s")
                except aiohttp.ClientError as exc:
                    provider_errors.append(f"{provider_name}=transport error: {_sanitize_rpc_error(exc)}")
                except Exception as exc:
                    provider_errors.append(f"{provider_name}=unexpected error: {_sanitize_rpc_error(exc)}")

    raise SwapError(
        f"RPC request failed after trying every available RealWallet execution provider "
        f"({context or 'unlabeled call'}). Provider diagnostics: "
        f"{'; '.join(provider_errors[-8:]) or 'no provider response recorded'}."
    )


async def get_quote(input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int = 150) -> dict:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "slippageBps": str(slippage_bps),
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(JUPITER_QUOTE_API, params=params, headers=_jupiter_headers(), timeout=15) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise SwapError(f"Jupiter quote failed ({resp.status}): {body[:200]}")
            return await resp.json()


async def build_swap_transaction(quote: dict, user_public_key: str, priority_fee_lamports: int | str = "auto") -> str:
    payload = {
        "quoteResponse": quote,
        "userPublicKey": user_public_key,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": priority_fee_lamports,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(JUPITER_SWAP_API, json=payload, headers=_jupiter_headers(), timeout=15) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise SwapError(f"Jupiter swap build failed ({resp.status}): {body[:200]}")
            data = await resp.json()
            tx_b64 = data.get("swapTransaction")
            if not tx_b64:
                raise SwapError("Jupiter response missing swapTransaction.")
            return tx_b64


async def _simulate_signed_transaction(signed_b64: str) -> dict:
    """Simulate the exact signed transaction and expose the failing instruction/program logs."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "simulateTransaction",
        "params": [
            signed_b64,
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "confirmed",
            },
        ],
    }
    diagnostics: list[str] = []
    async with aiohttp.ClientSession() as session:
        for provider_name, endpoint in _real_wallet_rpc_endpoints():
            try:
                async with session.post(
                    endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=_RPC_BROADCAST_TIMEOUT_S),
                ) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        diagnostics.append(f"{provider_name}=HTTP {resp.status}: {body[:300]}")
                        continue
                    data = await resp.json(content_type=None)
                    if data.get("error"):
                        diagnostics.append(f"{provider_name}=RPC error {_sanitize_rpc_error(data['error'])}")
                        continue
                    value = (data.get("result") or {}).get("value") or {}
                    err = value.get("err")
                    logs = value.get("logs") or []
                    if err:
                        instruction = None
                        custom_error = None
                        if isinstance(err, dict) and "InstructionError" in err:
                            pair = err["InstructionError"]
                            if isinstance(pair, list) and len(pair) == 2:
                                instruction = pair[0]
                                detail = pair[1]
                                if isinstance(detail, dict):
                                    custom_error = detail.get("Custom")
                        logger.error(
                            "[RealWallet] PRE-SEND SIMULATION REJECT provider=%s instruction=%s custom=%s logs=%s",
                            provider_name, instruction, custom_error, logs,
                        )
                        return {
                            "ok": False,
                            "provider": provider_name,
                            "err": err,
                            "logs": logs,
                            "instruction": instruction,
                            "custom_error": custom_error,
                        }
                    return {"ok": True, "provider": provider_name, "logs": logs}
            except Exception as exc:
                diagnostics.append(f"{provider_name}={_sanitize_rpc_error(exc)}")
    raise SwapError("RealWallet pre-send simulation could not be completed: " + "; ".join(diagnostics[-8:]))


async def _broadcast_signed_transaction(signed_b64: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            signed_b64,
            {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "maxRetries": 3,
            },
        ],
    }
    endpoints = _real_wallet_rpc_endpoints()
    if not endpoints:
        raise SwapError("RealWallet RPC is not configured: no Helius or QuickNode execution endpoint is available.")

    provider_errors: list[str] = []
    async with aiohttp.ClientSession() as session:
        for provider_name, endpoint in endpoints:
            for attempt in range(1, _RPC_BROADCAST_ATTEMPTS_PER_PROVIDER + 1):
                started = time.monotonic()
                try:
                    logger.info("[RealWallet] sendTransaction provider=%s attempt=%d/%d", provider_name, attempt, _RPC_BROADCAST_ATTEMPTS_PER_PROVIDER)
                    async with session.post(endpoint, json=payload, timeout=aiohttp.ClientTimeout(total=_RPC_BROADCAST_TIMEOUT_S)) as resp:
                        body = await resp.text()
                        elapsed_ms = (time.monotonic() - started) * 1000
                        if resp.status != 200:
                            diagnostic = f"HTTP {resp.status}: {body[:_MAX_RPC_ERROR_LOG_CHARS]}"
                            provider_errors.append(f"{provider_name}={_sanitize_rpc_error(diagnostic)}")
                            if resp.status in (400, 401, 403, 404):
                                break
                            continue
                        try:
                            data = await resp.json(content_type=None)
                        except Exception as exc:
                            provider_errors.append(f"{provider_name}=invalid JSON response: {_sanitize_rpc_error(exc)}")
                            continue
                        if isinstance(data, dict) and data.get("error") is not None:
                            rpc_error = data["error"]
                            provider_errors.append(f"{provider_name}=RPC error {_sanitize_rpc_error(rpc_error)}")
                            logger.warning("[RealWallet] sendTransaction provider=%s RPC_REJECT latency_ms=%.0f error=%s", provider_name, elapsed_ms, _sanitize_rpc_error(rpc_error))
                            break
                        signature = data.get("result") if isinstance(data, dict) else None
                        if signature:
                            logger.info("[RealWallet] sendTransaction accepted provider=%s latency_ms=%.0f signature_received=true", provider_name, elapsed_ms)
                            return signature
                        provider_errors.append(f"{provider_name}=HTTP 200 but RPC response contained no transaction signature")
                except asyncio.TimeoutError:
                    provider_errors.append(f"{provider_name}=timeout after {_RPC_BROADCAST_TIMEOUT_S}s")
                except aiohttp.ClientError as exc:
                    provider_errors.append(f"{provider_name}=transport error: {_sanitize_rpc_error(exc)}")
                except Exception as exc:
                    provider_errors.append(f"{provider_name}=unexpected error: {_sanitize_rpc_error(exc)}")

    raise SwapError(
        "RPC broadcast failed after trying the available RealWallet execution providers. "
        f"Provider diagnostics: {'; '.join(provider_errors[-8:]) or 'no provider response recorded'}. No transaction signature was returned."
    )


async def sign_and_send(tx_b64: str, secret_key_bytes: bytes) -> str:
    keypair = Keypair.from_bytes(secret_key_bytes)
    raw_tx = base64.b64decode(tx_b64)
    tx = VersionedTransaction.from_bytes(raw_tx)
    signed_tx = VersionedTransaction(tx.message, [keypair])
    signed_b64 = base64.b64encode(bytes(signed_tx)).decode()

    simulation = await _simulate_signed_transaction(signed_b64)
    if not simulation["ok"]:
        raise SwapError(
            "Transaction simulation failed before broadcast: "
            f"instruction={simulation.get('instruction')} "
            f"custom_error={simulation.get('custom_error')} "
            f"provider={simulation.get('provider')} "
            f"logs={simulation.get('logs') or []}"
        )
    return await _broadcast_signed_transaction(signed_b64)


async def confirm_signature(signature: str, timeout_s: float = CONFIRMATION_TIMEOUT_S, poll_interval_s: float = CONFIRMATION_POLL_INTERVAL_S) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignatureStatuses",
        "params": [[signature], {"searchTransactionHistory": True}],
    }
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data = await realwallet_rpc_call(payload, context=f"confirm_signature:{signature}")
        except SwapError as e:
            # A missed poll is not a failed confirmation — the transaction
            # is already broadcast and irreversible at this point; simply
            # retry on the next poll tick with (potentially) a healthier
            # provider rotation rather than giving up on confirmation.
            logger.warning("[RealWallet] confirm_signature poll failed, retrying: %s", e)
            data = None
        status = None
        if data is not None:
            statuses = (data.get("result") or {}).get("value") or [None]
            status = statuses[0]
        if status is not None:
            if status.get("err"):
                return {"status": "failed", "err": status["err"]}
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return {"status": "confirmed", "err": None}
        await asyncio.sleep(poll_interval_s)
    return {"status": "timeout", "err": None}


async def sign_send_and_confirm(tx_b64: str, secret_key_bytes: bytes, timeout_s: float = CONFIRMATION_TIMEOUT_S) -> dict:
    signature = await sign_and_send(tx_b64, secret_key_bytes)
    result = await confirm_signature(signature, timeout_s=timeout_s)
    result["signature"] = signature
    return result


async def get_confirmed_transaction_deltas(signature: str, owner_pubkey: str, mint: str) -> dict:
    """Read the ACTUAL on-chain result of a confirmed swap for `owner_pubkey`.

    A Jupiter quote's outAmount is a pre-trade estimate only. The amount
    that actually lands can differ from it - slippage tolerance, price
    movement between quote and landing, and route re-planning are all
    normal on Solana, especially for low-liquidity meme-coin pools. The
    only source of truth for what was actually bought or sold is the
    confirmed transaction's own balance deltas, so every accounting figure
    (token_quantity on a buy, sol_received on a sell, and therefore
    realized PnL) must be derived from this, never from the quote.

    Returns signed deltas for the owner:
      - sol_delta_lamports: native SOL balance change (post - pre), already
        net of the network/priority fee since both balances reflect it.
        wrapAndUnwrapSol=True means a sell's proceeds land here as native
        SOL after Jupiter auto-unwraps WSOL.
      - token_delta_raw: SPL token balance change (post - pre) in raw
        (un-decimalized) units for `mint`, owned by `owner_pubkey`.
      - fee_lamports: the network fee paid, for observability.

    Raises SwapError if the transaction cannot be fetched/parsed.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0},
        ],
    }
    data = await realwallet_rpc_call(payload, context=f"get_transaction:{signature}")
    result = data.get("result")
    if not result:
        raise SwapError(f"Transaction {signature} was not found when reading fill details.")

    meta = result.get("meta") or {}
    if meta.get("err"):
        raise SwapError(f"Transaction {signature} reverted on-chain: {meta['err']}")

    account_keys = [
        (k.get("pubkey") if isinstance(k, dict) else k)
        for k in ((result.get("transaction") or {}).get("message") or {}).get("accountKeys", [])
    ]
    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    sol_delta_lamports = 0
    if owner_pubkey in account_keys:
        idx = account_keys.index(owner_pubkey)
        if idx < len(pre_balances) and idx < len(post_balances):
            sol_delta_lamports = int(post_balances[idx]) - int(pre_balances[idx])

    def _token_raw_amount(entries) -> int:
        for entry in entries or []:
            if entry.get("owner") == owner_pubkey and entry.get("mint") == mint:
                try:
                    return int((entry.get("uiTokenAmount") or {}).get("amount", "0"))
                except (TypeError, ValueError):
                    return 0
        return 0

    pre_token_raw = _token_raw_amount(meta.get("preTokenBalances"))
    post_token_raw = _token_raw_amount(meta.get("postTokenBalances"))

    return {
        "sol_delta_lamports": sol_delta_lamports,
        "token_delta_raw": post_token_raw - pre_token_raw,
        "fee_lamports": int(meta.get("fee", 0)),
    }


async def get_mint_decimals(mint: str) -> int:
    cached = _MINT_DECIMALS_CACHE.get(mint)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]

    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]}
    data = await realwallet_rpc_call(payload, context=f"mint_decimals:{mint}")
    value = data.get("result", {}).get("value")
    if not value or "decimals" not in value:
        raise SwapError(f"Could not fetch decimals for mint {mint}.")
    decimals = int(value["decimals"])
    _MINT_DECIMALS_CACHE[mint] = (decimals, time.monotonic() + _MINT_DECIMALS_CACHE_TTL_SECONDS)
    return decimals


async def get_token_balance(public_key: str, mint: str) -> dict:
    """Return authoritative on-chain SPL balance for a mint, in raw units."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            public_key,
            {"mint": mint},
            {"encoding": "jsonParsed", "commitment": "confirmed"},
        ],
    }
    data = await realwallet_rpc_call(payload, context=f"token_balance:{public_key}:{mint}")

    accounts = ((data.get("result") or {}).get("value") or [])
    total_raw = 0
    decimals = None
    token_accounts = []
    for item in accounts:
        info = (((item.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        token_amount = info.get("tokenAmount") or {}
        try:
            raw = int(token_amount.get("amount", "0"))
        except (TypeError, ValueError):
            raw = 0
        total_raw += raw
        if decimals is None and token_amount.get("decimals") is not None:
            decimals = int(token_amount["decimals"])
        token_accounts.append({
            "address": item.get("pubkey"),
            "raw_amount": raw,
            "ui_amount": token_amount.get("uiAmountString"),
        })

    if decimals is None:
        decimals = await get_mint_decimals(mint)

    return {
        "mint": mint,
        "raw_amount": total_raw,
        "ui_amount": total_raw / (10 ** decimals),
        "decimals": decimals,
        "token_accounts": token_accounts,
    }


async def get_sol_balance(public_key: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [public_key]}
    data = await realwallet_rpc_call(payload, context=f"sol_balance:{public_key}")
    result = data.get("result")
    if not isinstance(result, dict) or "value" not in result:
        raise SwapError("Balance RPC returned an unexpected response.")
    return result["value"] / 1_000_000_000
