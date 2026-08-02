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

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.models import TokenAccountOpts
from solders.pubkey import Pubkey

from whale_alpha.config import Env
from whale_alpha.utils.logger import child_logger

log = child_logger("solana_connection")


def _redact_endpoint(url: str) -> str:
    """Strips likely API keys from an RPC URL before it ever hits a log line
    — query strings, and (Alchemy/Ankr/dRPC-style) the last path segment.
    """
    parsed = urlsplit(url)
    path = parsed.path
    segments = path.rstrip("/").split("/")
    if len(segments) > 1 and segments[-1]:
        segments[-1] = "***"
        path = "/".join(segments)
    query = "***" if parsed.query else ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def _resolve_rpc_endpoints(env: Env) -> list[str]:
    """Builds the ordered list of Solana RPC endpoints to use, primary first.

    SOLANA_RPC_URL is always tried first (unchanged behavior when nothing
    else is configured). Everything after it is only ever tried once an
    earlier endpoint has failed:

      1. SOLANA_RPC_FALLBACK_URLS — comma-separated full URLs, for any
         provider not covered below (or a self-hosted / private node).
      2. DRPC_API_KEY / ALCHEMY_API_KEY / ANKR_API_KEY — convenience keys
         that build the provider's standard Solana mainnet URL, so ops
         don't have to hand-construct it.
      3. On mainnet-beta only: a couple of free, keyless public endpoints
         (dRPC, Ankr) as a last-resort safety net, so a missing/misconfigured
         API key still leaves the bot with *some* redundancy rather than a
         single point of failure. These have low rate limits — fine as a
         fallback of last resort, not meant to carry sustained load.

    Duplicates (e.g. the same URL listed twice, or repeated across
    SOLANA_RPC_URL and a fallback) are dropped, preserving first occurrence
    so the intended priority order is kept.
    """
    endpoints = [env.SOLANA_RPC_URL]

    for raw in env.SOLANA_RPC_FALLBACK_URLS.split(","):
        url = raw.strip()
        if url:
            endpoints.append(url)

    if env.DRPC_API_KEY:
        endpoints.append(f"https://solana.drpc.org?dkey={env.DRPC_API_KEY}")
    if env.ALCHEMY_API_KEY:
        endpoints.append(f"https://solana-mainnet.g.alchemy.com/v2/{env.ALCHEMY_API_KEY}")
    if env.ANKR_API_KEY:
        endpoints.append(f"https://rpc.ankr.com/solana/{env.ANKR_API_KEY}")

    if env.SOLANA_CLUSTER == "mainnet-beta":
        endpoints.append("https://solana.drpc.org")
        endpoints.append("https://rpc.ankr.com/solana")

    seen: set[str] = set()
    deduped: list[str] = []
    for url in endpoints:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


class _FailoverAsyncClient:
    """Duck-typed drop-in for `solana.rpc.async_api.AsyncClient` that
    transparently fails over across multiple RPC endpoints.

    Every attribute access (`.get_balance`, `.send_raw_transaction`,
    `.get_signatures_for_address`, ...) returns a proxy coroutine: calling it
    attempts the currently-active endpoint's real `AsyncClient` first, then —
    only if that raises — each remaining endpoint in turn, up to
    `max_attempts` total tries, before re-raising the last error. This means
    every existing call site across the codebase (`connection.get_balance(...)`,
    `await connection.close()`, the `fn` passed into `_rate_limited_rpc_call`,
    etc.) keeps working completely unchanged; only `create_connection()`
    needed to change.

    Failover is "sticky": once an endpoint succeeds, it becomes the active
    one for subsequent calls (rather than always retrying the primary
    first), so a sustained primary-provider outage doesn't tack a doomed
    extra round-trip onto every single call until it recovers.

    Re-sending a transaction to a second RPC after `send_raw_transaction`
    raises on the first is safe: Solana dedupes by transaction signature, so
    a possible double-broadcast of the *same* signed transaction is a no-op
    at worst (and is exactly what some production setups do deliberately,
    for faster propagation).
    """

    def __init__(self, urls: list[str], *, max_attempts: int, commitment: Any = Confirmed) -> None:
        self._urls = urls
        self._clients = [AsyncClient(url, commitment=commitment) for url in urls]
        self._max_attempts = min(max_attempts, len(urls))
        self._active = 0

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def _failover_call(*args: Any, **kwargs: Any) -> Any:
            last_err: Exception | None = None
            index = self._active
            for attempt in range(self._max_attempts):
                client = self._clients[index]
                try:
                    result = await getattr(client, name)(*args, **kwargs)
                except Exception as err:  # noqa: BLE001 — any provider-side failure triggers failover
                    last_err = err
                    log.warning(
                        "Solana RPC call failed, trying next endpoint" if attempt + 1 < self._max_attempts
                        else "Solana RPC call failed on all endpoints",
                        method=name,
                        endpoint=_redact_endpoint(self._urls[index]),
                        err=str(err),
                        err_type=type(err).__name__,
                    )
                    index = (index + 1) % len(self._clients)
                    continue
                if index != self._active:
                    log.warning(
                        "Solana RPC failover: switched active endpoint",
                        method=name,
                        new_endpoint=_redact_endpoint(self._urls[index]),
                    )
                    self._active = index
                return result

            assert last_err is not None
            raise last_err

        return _failover_call

    async def close(self) -> None:
        for client in self._clients:
            with contextlib.suppress(Exception):  # noqa: BLE001 — best-effort cleanup of every endpoint
                await client.close()


def create_connection(env: Env) -> AsyncClient:
    endpoints = _resolve_rpc_endpoints(env)
    if len(endpoints) == 1:
        return AsyncClient(endpoints[0], commitment=Confirmed)
    log.info(
        "Solana RPC configured with failover",
        endpoints=[_redact_endpoint(url) for url in endpoints],
        max_attempts=min(env.SOLANA_RPC_MAX_FAILOVER_ATTEMPTS, len(endpoints)),
    )
    return _FailoverAsyncClient(  # type: ignore[return-value] — duck-typed, see class docstring
        endpoints, max_attempts=env.SOLANA_RPC_MAX_FAILOVER_ATTEMPTS
    )


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


async def get_token_largest_accounts(
    connection: AsyncClient,
    mint: str,
    limit: int = 20,
    *,
    min_interval_seconds: float = 0.12,
    max_retries: int = 3,
) -> list[str]:
    """Returns up to `limit` owner addresses holding the largest balances of
    `mint`, via plain RPC `getTokenLargestAccounts` (no indexer needed).

    Used as the discovery engine's candidate sources (see
    integrations/wallet_discovery_source.py): both for holders of a token
    that already produced a Signal, and for holders of Jupiter's
    platform-wide trending tokens. `getTokenLargestAccounts` returns *token
    accounts*, not owners directly, so each is resolved via `getAccountInfo`
    (jsonParsed) to its owning wallet — up to 21 RPC calls for one mint (1 +
    up to 20, the RPC's own hard cap on this call; `limit` only trims
    further, it cannot request more than the RPC returns).

    RATE LIMITING (fixes a real production issue): the discovery engine can
    call this for ~20 trending tokens in one cycle, which without any
    pacing means ~400+ RPC calls fired back-to-back — comfortably over what
    most providers' free/lower tiers allow (Helius's RPC endpoint included),
    producing a wall of 429s and silently dropping most candidates. Every
    call here (including the initial getTokenLargestAccounts) is paced to
    at least `min_interval_seconds` apart via a process-wide gate, and
    retries once-or-more with exponential backoff specifically on 429 —
    other errors (a bad/closed account, a malformed mint) still fail fast
    and get skipped per-account as before, since retrying those wouldn't help.
    """
    owners: list[str] = []
    resp = await _rate_limited_rpc_call(
        connection.get_token_largest_accounts,
        Pubkey.from_string(mint),
        min_interval_seconds=min_interval_seconds,
        max_retries=max_retries,
    )
    token_accounts = [entry.address for entry in resp.value][:limit]

    for token_account in token_accounts:
        try:
            info = await _rate_limited_rpc_call(
                connection.get_account_info_json_parsed,
                token_account,
                min_interval_seconds=min_interval_seconds,
                max_retries=max_retries,
            )
            parsed = info.value.data.parsed
            owner = parsed["info"]["owner"]
            if owner:
                owners.append(owner)
        except Exception:  # noqa: BLE001 — skip an unparseable/closed/still-rate-limited account
            continue
    return owners


_rpc_pacing_lock = asyncio.Lock()
_rpc_last_call_at = 0.0


async def _rate_limited_rpc_call(
    fn: Callable[..., Awaitable[Any]], *args: Any, min_interval_seconds: float, max_retries: int, **kwargs: Any
) -> Any:
    """Process-wide pacing gate (at most one RPC call per `min_interval_seconds`
    across ALL callers of this helper, not per-mint or per-connection —
    that's the point, since the 429s come from the shared provider-side
    limit, not a per-call one) plus retry-with-backoff specifically for 429
    responses. Non-429 errors propagate immediately, unretried.
    """
    attempt = 0
    while True:
        async with _rpc_pacing_lock:
            global _rpc_last_call_at
            wait = min_interval_seconds - (time.monotonic() - _rpc_last_call_at)
            if wait > 0:
                await asyncio.sleep(wait)
            _rpc_last_call_at = time.monotonic()

        try:
            return await fn(*args, **kwargs)
        except Exception as err:  # noqa: BLE001 — inspected below; re-raised if not a 429
            attempt += 1
            if not _is_rate_limited_error(err) or attempt > max_retries:
                raise
            backoff = min_interval_seconds * (2**attempt)
            await asyncio.sleep(backoff)


def _is_rate_limited_error(err: Exception) -> bool:
    """Best-effort 429 detection across whatever HTTP client the installed
    solana-py version vendors internally (it has historically shipped its
    own patched httpx under a different import name to avoid version
    conflicts) — so this checks status_code via duck-typing first, and
    falls back to string matching if that attribute chain isn't present.
    """
    response = getattr(err, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 429:
        return True
    text = str(err)
    return "429" in text or "Too Many Requests" in text


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
