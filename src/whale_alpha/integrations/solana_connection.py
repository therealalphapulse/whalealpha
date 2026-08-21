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
import json
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


# --------------------------------------------------------------------------
# Provider resolution
# --------------------------------------------------------------------------
#
# Every configured RPC endpoint is tagged with a `role`, which is what
# workload routing (below) uses to decide which provider a given RPC method
# should prefer:
#
#   * "primary"   — SOLANA_RPC_URL. The provider ops explicitly chose as
#                    the bot's main node — typically the most reliable/paid
#                    one (in this deployment's logs, Helius's RPC endpoint).
#                    Preferred for latency- and correctness-sensitive
#                    workloads (landing + confirming transactions, reading
#                    signature/transaction history for reconciliation).
#   * "secondary" — SOLANA_RPC_FALLBACK_URLS entries and DRPC_API_KEY /
#                    ALCHEMY_API_KEY / ANKR_API_KEY. Ops-provisioned
#                    redundancy. Preferred for high-volume commodity reads
#                    (balance/account/token-holder lookups) specifically to
#                    take that load off the primary node — the discovery
#                    engine alone can fire hundreds of these in one cycle
#                    (see get_token_largest_accounts below), which is the
#                    exact traffic pattern that produced the 429s this
#                    module was originally hardened against.
#   * "public"    — free, keyless mainnet-beta safety-net endpoints added
#                    automatically. Last resort only in every workload:
#                    low rate limits, fine as a final fallback, not meant
#                    to carry real traffic.


class _RpcProviderSpec:
    __slots__ = ("name", "url", "role")

    def __init__(self, name: str, url: str, role: str) -> None:
        self.name = name
        self.url = url
        self.role = role


def _resolve_rpc_provider_specs(env: Env) -> list[_RpcProviderSpec]:
    """Builds the ordered, deduplicated, role-tagged list of Solana RPC
    providers to use. SOLANA_RPC_URL is always included first as "primary"
    (unchanged behavior when nothing else is configured). Everything after
    it is only ever tried once an earlier provider in its workload's route
    has failed — see `_build_routes` for how role maps to per-workload
    ordering:

      1. SOLANA_RPC_FALLBACK_URLS — comma-separated full URLs, for any
         provider not covered below (or a self-hosted / private node).
      2. DRPC_API_KEY / ALCHEMY_API_KEY / ANKR_API_KEY — convenience keys
         that build the provider's standard Solana mainnet URL, so ops
         don't have to hand-construct it.
      3. On mainnet-beta only: a couple of free, keyless public endpoints
         (dRPC, Ankr) as a last-resort safety net, so a missing/misconfigured
         API key still leaves the bot with *some* redundancy rather than a
         single point of failure.

    Duplicates (e.g. the same URL listed twice, or repeated across
    SOLANA_RPC_URL and a fallback) are dropped, preserving first occurrence
    so the intended priority order is kept.
    """
    specs = [_RpcProviderSpec("primary", env.SOLANA_RPC_URL, "primary")]

    for i, raw in enumerate(env.SOLANA_RPC_FALLBACK_URLS.split(",")):
        url = raw.strip()
        if url:
            specs.append(_RpcProviderSpec(f"fallback-{i}", url, "secondary"))

    if env.DRPC_API_KEY:
        specs.append(_RpcProviderSpec("drpc", f"https://solana.drpc.org?dkey={env.DRPC_API_KEY}", "secondary"))
    if env.ALCHEMY_API_KEY:
        specs.append(
            _RpcProviderSpec(
                "alchemy", f"https://solana-mainnet.g.alchemy.com/v2/{env.ALCHEMY_API_KEY}", "secondary"
            )
        )
    if env.ANKR_API_KEY:
        specs.append(_RpcProviderSpec("ankr", f"https://rpc.ankr.com/solana/{env.ANKR_API_KEY}", "secondary"))

    if env.SOLANA_CLUSTER == "mainnet-beta":
        specs.append(_RpcProviderSpec("drpc-public", "https://solana.drpc.org", "public"))
        specs.append(_RpcProviderSpec("ankr-public", "https://rpc.ankr.com/solana", "public"))

    seen: set[str] = set()
    deduped: list[_RpcProviderSpec] = []
    for spec in specs:
        if spec.url not in seen:
            seen.add(spec.url)
            deduped.append(spec)
    return deduped


def resolve_websocket_url(env: Env) -> str | None:
    """Picks the best available Solana WebSocket endpoint, for future use by
    a WS-based wallet monitor.

    NOT currently called by any engine — see this module's top-of-file TODO
    and engines/monitor.py, which still polls RPC rather than subscribing.
    Provided now so that work doesn't also have to re-derive "which
    configured provider has WS support"; adding it here is purely additive
    and does not change any existing behavior.

    Preference order: an explicitly configured SOLANA_WS_URL always wins
    (ops' own choice); otherwise the first WS-capable *keyed* provider
    (Alchemy, then dRPC — both support `wss://` on the same host/key as
    their HTTP RPC endpoint; Ankr's Solana WS support requires a
    provider-specific path this module doesn't guess at, so it's skipped
    here). Returns None if nothing WS-capable is configured.
    """
    if env.SOLANA_WS_URL:
        return env.SOLANA_WS_URL
    if env.ALCHEMY_API_KEY:
        return f"wss://solana-mainnet.g.alchemy.com/v2/{env.ALCHEMY_API_KEY}"
    if env.DRPC_API_KEY:
        return f"wss://solana.drpc.org?dkey={env.DRPC_API_KEY}"
    return None


# --------------------------------------------------------------------------
# Workload routing
# --------------------------------------------------------------------------

# Solana RPC method name -> workload category. Anything not listed here
# (get_token_supply's sibling calls added by a future solana-py version,
# etc.) falls back to "general", which routes exactly like the original
# flat failover (primary first) — so an unrecognized method never loses
# redundancy, it just doesn't get workload-specific placement.
_METHOD_WORKLOADS: dict[str, str] = {
    # account / balance lookups
    "get_balance": "account_lookup",
    "get_account_info": "account_lookup",
    "get_account_info_json_parsed": "account_lookup",
    "get_token_accounts_by_owner_json_parsed": "account_lookup",
    # token data ("token metadata" in the RPC methods this codebase actually
    # calls — decimals/supply and largest-holder resolution; on-chain
    # Metaplex metadata lookups aren't used anywhere in this repo)
    "get_token_supply": "token_metadata",
    "get_token_largest_accounts": "token_metadata",
    # transaction / signature history
    "get_signatures_for_address": "tx_history",
    "get_signature_statuses": "tx_history",
    "get_transaction": "tx_history",
    "get_block_time": "tx_history",
    # transaction submission + the chain state needed to submit/confirm one
    "send_raw_transaction": "tx_submission",
    "confirm_transaction": "tx_submission",
    "get_block_height": "tx_submission",
    "get_latest_blockhash": "tx_submission",
    "is_blockhash_valid": "tx_submission",
}

# Per-workload provider-role preference. Read as: for this workload, try
# every provider with the first listed role, then every provider with the
# second role, etc. (Within a role, providers keep the relative order they
# were resolved in — SOLANA_RPC_FALLBACK_URLS order, then dRPC/Alchemy/Ankr.)
_ROLE_PRIORITY_BY_WORKLOAD: dict[str, tuple[str, ...]] = {
    "tx_submission": ("primary", "secondary", "public"),
    "tx_history": ("primary", "secondary", "public"),
    "account_lookup": ("secondary", "primary", "public"),
    "token_metadata": ("secondary", "primary", "public"),
    "general": ("primary", "secondary", "public"),
}

# NOTE on price queries: they're intentionally out of scope for this
# module's routing. Price lookups never went through Solana RPC at all —
# they're a separate Jupiter Price API client (integrations/price_feed.py)
# with its own PRICE_FEED_API_BASE/PRICE_FEED_API_KEY override and its own
# fallback behavior. dRPC/Alchemy/Ankr are Solana RPC node providers, not
# price oracles, so there's nothing to route price queries to among them.


class _FailoverAsyncClient:
    """Duck-typed drop-in for `solana.rpc.async_api.AsyncClient` that routes
    each RPC method to whichever configured provider best suits its
    workload (see `_ROLE_PRIORITY_BY_WORKLOAD` above), and transparently
    fails over to the next provider in that workload's route if the
    assigned one errors.

    Every attribute access (`.get_balance`, `.send_raw_transaction`,
    `.get_signatures_for_address`, ...) returns a proxy coroutine. This
    means every existing call site across the codebase
    (`connection.get_balance(...)`, `await connection.close()`, the `fn`
    passed into `_rate_limited_rpc_call`, etc.) keeps working completely
    unchanged; only `create_connection()` needed to change.

    Routing is "sticky" *per workload*: once a provider succeeds for a given
    workload, it stays the active one for that workload's future calls
    (rather than re-trying the whole route from the top every time) — so,
    for example, transaction submission can stay pinned to the primary node
    while account lookups simultaneously stay pinned to a secondary
    provider, and a sustained single-provider outage doesn't tack a doomed
    extra round-trip onto every subsequent call in that workload until it
    recovers. Different workloads track their active provider
    independently.

    Re-sending a transaction to a second RPC after `send_raw_transaction`
    raises on the first is safe: Solana dedupes by transaction signature, so
    a possible double-broadcast of the *same* signed transaction is a no-op
    at worst (and is exactly what some production setups do deliberately,
    for faster propagation).
    """

    def __init__(
        self,
        specs: list[_RpcProviderSpec],
        *,
        max_attempts: int,
        routing_strategy: str,
        commitment: Any = Confirmed,
    ) -> None:
        self._names = [spec.name for spec in specs]
        self._urls = [spec.url for spec in specs]
        self._clients = [AsyncClient(spec.url, commitment=commitment) for spec in specs]
        self._max_attempts = min(max_attempts, len(specs))
        self._routes = self._build_routes(specs, routing_strategy)
        self._active: dict[str, int] = {category: order[0] for category, order in self._routes.items()}

    @staticmethod
    def _build_routes(specs: list[_RpcProviderSpec], routing_strategy: str) -> dict[str, list[int]]:
        natural_order = list(range(len(specs)))

        if routing_strategy == "primary_first":
            # Opt-out of workload-aware routing: every workload gets the
            # same route, primary first — i.e. exactly the original flat
            # failover behavior, for ops who'd rather not have different
            # workloads pinned to different providers.
            return {category: natural_order for category in {*_METHOD_WORKLOADS.values(), "general"}}

        indices_by_role: dict[str, list[int]] = {"primary": [], "secondary": [], "public": []}
        for index, spec in enumerate(specs):
            indices_by_role[spec.role].append(index)

        routes: dict[str, list[int]] = {}
        for category, role_priority in _ROLE_PRIORITY_BY_WORKLOAD.items():
            order = [index for role in role_priority for index in indices_by_role[role]]
            routes[category] = order or natural_order
        return routes

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        category = _METHOD_WORKLOADS.get(name, "general")

        async def _routed_call(*args: Any, **kwargs: Any) -> Any:
            order = self._routes[category]
            active = self._active[category]
            start_pos = order.index(active) if active in order else 0
            # This workload's preferred route, rotated to start at whichever
            # provider is currently active for it (sticky), then capped to
            # max_attempts so a fully-down provider set fails fast rather
            # than exhausting every configured endpoint on every call.
            route = (order[start_pos:] + order[:start_pos])[: self._max_attempts]

            last_err: Exception | None = None
            for position, index in enumerate(route):
                client = self._clients[index]
                try:
                    result = await getattr(client, name)(*args, **kwargs)
                except Exception as err:  # noqa: BLE001 — any provider-side failure triggers failover
                    last_err = err
                    log.warning(
                        "Solana RPC call failed, routing to next provider"
                        if position + 1 < len(route)
                        else "Solana RPC call failed on every routed provider",
                        method=name,
                        workload=category,
                        provider=self._names[index],
                        endpoint=_redact_endpoint(self._urls[index]),
                        err=str(err),
                        err_type=type(err).__name__,
                    )
                    continue
                if index != self._active[category]:
                    log.warning(
                        "Solana RPC routing: switched active provider for workload",
                        method=name,
                        workload=category,
                        new_provider=self._names[index],
                        new_endpoint=_redact_endpoint(self._urls[index]),
                    )
                    self._active[category] = index
                return result

            assert last_err is not None
            raise last_err

        return _routed_call

    async def close(self) -> None:
        for client in self._clients:
            with contextlib.suppress(Exception):  # noqa: BLE001 — best-effort cleanup of every endpoint
                await client.close()


def create_connection(env: Env) -> AsyncClient:
    specs = _resolve_rpc_provider_specs(env)
    if len(specs) == 1:
        return AsyncClient(specs[0].url, commitment=Confirmed)

    connection = _FailoverAsyncClient(
        specs,
        max_attempts=env.SOLANA_RPC_MAX_FAILOVER_ATTEMPTS,
        routing_strategy=env.SOLANA_RPC_ROUTING_STRATEGY,
    )
    log.info(
        "Solana RPC configured with multi-provider routing",
        providers=[
            {"name": spec.name, "role": spec.role, "endpoint": _redact_endpoint(spec.url)} for spec in specs
        ],
        routing_strategy=env.SOLANA_RPC_ROUTING_STRATEGY,
        max_attempts=connection._max_attempts,  # noqa: SLF001 — logging our own just-built instance
    )
    return connection  # type: ignore[return-value] — duck-typed, see class docstring




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


async def get_token_first_seen_at_ms(connection: AsyncClient, mint: str) -> int | None:
    """Return a bounded on-chain first-seen timestamp for a Solana mint.

    ``getSignaturesForAddress`` is newest-first and capped at 1,000 rows per call.
    We page a small bounded number of times and only accept the oldest signature
    when the final page is complete, so a truncated history cannot masquerade as
    the mint's true creation time and accidentally bypass the maximum-age gate.
    """
    try:
        pubkey = Pubkey.from_string(mint)
        before = None
        oldest = None
        for _ in range(3):
            kwargs: dict[str, Any] = {
                "limit": 1000,
                "min_interval_seconds": 0.12,
                "max_retries": 3,
            }
            if before is not None:
                kwargs["before"] = before
            resp = await _rate_limited_rpc_call(
                connection.get_signatures_for_address,
                pubkey,
                **kwargs,
            )
            values = list(resp.value or [])
            if not values:
                return None
            oldest = values[-1]
            if len(values) < 1000:
                break
            before = oldest.signature
        else:
            return None
    except Exception as err:  # noqa: BLE001 — best-effort fallback
        log.debug("RPC token age lookup failed", mint=mint, err=str(err))
        return None

    block_time = getattr(oldest, "block_time", None) if oldest is not None else None
    if block_time is None:
        return None
    try:
        return int(block_time) * 1000
    except (TypeError, ValueError):
        return None


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


async def get_wallet_recent_transactions(
    connection: AsyncClient,
    address: str,
    *,
    max_signatures: int = 40,
    min_interval_seconds: float = 0.12,
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    """Best-effort transaction history via plain RPC (`getSignaturesForAddress`
    + `getTransaction`, jsonParsed) — the wallet-history fallback used when
    Helius Enhanced Transactions is unavailable/rate-limited (see
    integrations/wallet_discovery_source.py's PRIMARY -> stale cache -> RPC
    fallback -> retry queue chain). Returns each transaction's raw
    jsonParsed `meta`/`transaction` payload as a plain dict; this module has
    no notion of a "swap" — wallet_discovery_source.py diffs the pre/post
    token & SOL balances itself to reconstruct BUY/SELL events.

    Routes through whatever RPC provider `connection` currently has as
    primary — the same Helius RPC / DRPC / Alchemy / Ankr failover this repo
    already uses for every other RPC call (see `_FailoverAsyncClient`
    above), so this genuinely is a *different* data path from the Helius
    Enhanced Transactions HTTP API even when Helius RPC happens to be that
    failover's primary — a REST-API outage/rate-limit and a JSON-RPC one are
    independent failure modes.

    Paced/retried exactly like every other RPC call in this module (see
    `_rate_limited_rpc_call`) — one fallback lookup here is up to
    `1 + max_signatures` RPC calls, so `max_signatures` is deliberately
    capped low by default; this is a resilience fallback, not a full history
    backfill.

    Never fabricates: a signature or transaction that fails to fetch/parse
    is skipped, not synthesized. Returns `[]` (never raises) so one wallet's
    RPC hiccup can't stop the discovery cycle, matching every other function
    in this module.
    """
    try:
        pubkey = Pubkey.from_string(address)
    except Exception:  # noqa: BLE001 — malformed address, nothing to reconstruct
        return []

    try:
        sigs_resp = await _rate_limited_rpc_call(
            connection.get_signatures_for_address,
            pubkey,
            limit=max_signatures,
            min_interval_seconds=min_interval_seconds,
            max_retries=max_retries,
        )
    except Exception as err:  # noqa: BLE001 — one wallet's history failing shouldn't stop the batch
        log.debug("RPC history fallback: get_signatures_for_address failed", address=address, err=str(err))
        return []

    signatures = [entry.signature for entry in (sigs_resp.value or []) if not entry.err]
    transactions: list[dict[str, Any]] = []
    for signature in signatures:
        try:
            tx_resp = await _rate_limited_rpc_call(
                connection.get_transaction,
                signature,
                max_supported_transaction_version=0,
                encoding="jsonParsed",
                min_interval_seconds=min_interval_seconds,
                max_retries=max_retries,
            )
        except Exception as err:  # noqa: BLE001 — skip this one signature, keep going
            log.debug(
                "RPC history fallback: get_transaction failed", address=address, signature=str(signature), err=str(err)
            )
            continue
        if tx_resp is None or tx_resp.value is None:
            continue
        try:
            parsed = json.loads(tx_resp.value.to_json())
        except Exception:  # noqa: BLE001 — unparseable payload, skip rather than guess
            continue
        if isinstance(parsed, dict):
            transactions.append(parsed)
    return transactions


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
