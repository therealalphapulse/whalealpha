import logging
from collections import defaultdict
from dataclasses import dataclass

try:
    from config.settings import HELIUS_HOLDER_CACHE_TTL_SECONDS
except ImportError:
    HELIUS_HOLDER_CACHE_TTL_SECONDS = 420.0

try:
    from config.settings import HELIUS_HOLDER_EMPTY_CACHE_TTL_SECONDS
except ImportError:
    HELIUS_HOLDER_EMPTY_CACHE_TTL_SECONDS = 20.0

from providers.rpc.helius_request_manager import helius_manager, PRIORITY_LOW
from providers.cache import get_cache
from providers.marketdata.solanatracker import get_bundle_risk_pct

try:
    from config.settings import BUNDLE_RISK_SOLANA_TRACKER_ENABLED
except ImportError:
    BUNDLE_RISK_SOLANA_TRACKER_ENABLED = True

logger = logging.getLogger("AlphaPulse.Holders")

# Classic SPL Token program. Covers the overwhelming majority of Solana
# memecoins/Pump.fun launches (this bot's actual target population); Token-
# 2022 mints are a known, deliberately out-of-scope gap — see note below.
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Safety cap on how many holder accounts we'll process from a single
# getProgramAccounts response. Unlike Helius's old DAS getTokenAccounts
# endpoint, standard getProgramAccounts has no cursor-based pagination — it
# returns every matching account in one response — so this is a client-side
# truncation rather than a page-count loop. 5,000 comfortably covers real
# early-stage Pump.fun tokens (confirmed case: 1,300 real holders vs. 1,000
# previously reported under the old paginated limit).
MAX_HOLDER_ACCOUNTS = 5000

# Wallets whose balance sits within this fraction of each other are grouped
# as a "bundle" cluster. This mirrors the surface-level heuristic used by
# GMGN/AVE-style bundle detectors: it flags wallets that look like they were
# provisioned together by the same actor (e.g. a sniper/insider bundle at
# launch), not a cryptographic proof of common ownership. Full funding-graph
# tracing (shared source wallet, same-block creation) would need archival
# transaction history beyond what this free-tier lookup does.
BUNDLE_BALANCE_TOLERANCE = 0.15  # holdings within 15% of each other
MIN_BUNDLE_CLUSTER_SIZE = 3

# Wallets holding less than this fraction of tracked supply are excluded
# from bundle/Sybil clustering entirely. Below this threshold, "several
# wallets within 15% of each other" is dominated by coincidence (many
# unrelated retail buyers/sniper bots independently using the same
# default/round buy size — e.g. everyone spending exactly 0.1-0.5 SOL on a
# fresh Pump.fun mint) rather than evidence of coordinated provisioning.
# This was the primary source of false Sybil/bundle flags: dust-tier
# wallets that don't move concentration risk in any meaningful way were
# being clustered purely on coincidental amount similarity. Cutting them
# out of the *clustering input* (they're still counted in total_holders)
# preserves detection of what actually matters — a handful of large
# wallets provisioned with near-identical stacks — while removing the
# noise floor.
MIN_BUNDLE_WALLET_SUPPLY_PCT = 0.05  # ignore wallets under 0.05% of supply

# How many top holder addresses to expose for downstream smart-money /
# tracked-whale cross-referencing (services/kol_tracker.py,
# services/whale_tracker.py). Capped so a single scan-cycle candidate never
# triggers an oversized DB "IN (...)" lookup.
MAX_HOLDER_ADDRESSES_EXPOSED = 100

# --- Provider-health diagnostic for the "0 raw holder account entries"
# state (see get_holder_analysis) ---
#
# A genuinely brand-new Pump.fun mint returning zero holder accounts is
# expected sometimes. But if EVERY (or nearly every) candidate — including
# established tokens with real volume/mcap — hits this state, that's not
# "these are all early tokens", it's a provider that isn't actually
# executing the getProgramAccounts scan (e.g. a plan/add-on restriction
# that returns an empty set instead of an error). Track a rolling window
# of outcomes so this failure mode surfaces itself in logs instead of
# silently looking like normal early-token behavior forever.
_EMPTY_RESULT_WINDOW: list[bool] = []  # True = zero accounts returned
_EMPTY_RESULT_WINDOW_SIZE = 20
_EMPTY_RESULT_ALERT_THRESHOLD = 0.90  # fraction empty that triggers a warning
_EMPTY_RESULT_ALERT_MIN_SAMPLES = 10  # don't judge off a tiny window


def _record_holder_fetch_outcome(contract_address: str, was_empty: bool) -> None:
    _EMPTY_RESULT_WINDOW.append(was_empty)
    if len(_EMPTY_RESULT_WINDOW) > _EMPTY_RESULT_WINDOW_SIZE:
        _EMPTY_RESULT_WINDOW.pop(0)

    if len(_EMPTY_RESULT_WINDOW) < _EMPTY_RESULT_ALERT_MIN_SAMPLES:
        return

    empty_rate = sum(_EMPTY_RESULT_WINDOW) / len(_EMPTY_RESULT_WINDOW)
    if empty_rate >= _EMPTY_RESULT_ALERT_THRESHOLD:
        logger.warning(
            f"[HolderDiag] ALERT: {empty_rate:.0%} of the last "
            f"{len(_EMPTY_RESULT_WINDOW)} holder-account lookups returned "
            f"zero accounts (most recently {contract_address[:8]}). This is "
            f"unlikely to mean every recent candidate is a genuinely "
            f"brand-new token — check whether the current RPC provider "
            f"actually supports/enables getProgramAccounts scans for this "
            f"filter (plan restriction, missing add-on, or silent partial "
            f"response), rather than treating this as normal early-token "
            f"coverage."
        )


@dataclass
class _HolderAccountsResult:
    accounts: list[dict]
    truncated: bool
    raw_account_count: int  # count actually returned by the RPC, pre-truncation


def _holder_cache_key(contract_address: str) -> str:
    # providers.cache.RedisCache already namespaces keys under "ap:", so
    # nothing else is needed here for collision-safety. Keeping the raw
    # address (rather than a hash of it) means `KEYS holder_accounts:*`
    # is still readable during an incident instead of opaque hex.
    return "holder_accounts:" + contract_address


# --- Primary holder-retrieval path: Helius getProgramAccountsV2 ---
#
# Root cause of the "0 raw holder accounts / HTTP 200" production failure:
# plain `getProgramAccounts` scoped to the SPL Token Program (a program
# with tens of millions of accounts) is a well-documented failure mode for
# every major Solana RPC provider, Helius included — Helius's own docs
# describe such calls as "notoriously plagued with issues" and note they
# are "sometimes outright banned... if the results aren't already cached"
# for large programs. Because this returns HTTP 200 with a syntactically
# valid empty array rather than an explicit error, the existing
# cross-provider empty-result validation (multi_rpc_manager._dispatch)
# could not distinguish it from a genuinely brand-new token: every
# provider in the chain hits the same structural limitation on this exact
# call, so "confirmed empty across N providers" was, in practice, often
# "N providers that all can't run this scan", not a legitimate signal.
#
# getProgramAccountsV2 is Helius's purpose-built replacement for exactly
# this case: cursor-based pagination designed for large-account-set
# programs, explicitly recommended by Helius for "applications dealing
# with programs that own large numbers of accounts (10,000+)". It is
# Helius-proprietary (see _HELIUS_ONLY_METHODS in multi_rpc_manager.py),
# so there is no cross-provider validation available for it — instead,
# _fetch_token_accounts falls back to the legacy multi-provider
# getProgramAccounts path (which DOES cross-validate) whenever V2 is
# unreachable, errors, or returns an unconfirmed-empty first page. This
# keeps the full Helius -> QuickNode -> Alchemy failover chain as a safety
# net without giving up the reliability win V2 provides for the common
# case.
_V2_PAGE_LIMIT = 10000  # Helius's documented max page size for getProgramAccountsV2
_V2_MAX_PAGES = 5       # safety cap: 5 * 10,000 = 50,000 raw entries — far beyond any
                         # real Pump.fun-era token; bounds worst-case latency instead
                         # of looping indefinitely on a runaway paginationKey.


async def _fetch_via_program_accounts_v2(
    contract_address: str, priority: int
) -> list[dict] | None:
    """
    Fetch raw holder account entries via Helius's getProgramAccountsV2.

    Returns the full list of raw account entries (same shape as a
    standard getProgramAccounts "result" array — a list of
    {"pubkey", "account": {...}} objects), or None if this path can't be
    trusted for this call and the caller should fall back to the legacy
    getProgramAccounts chain (Helius unreachable/not configured, an
    error response, an unexpected response shape, or an unconfirmed-empty
    first page).
    """
    all_accounts: list[dict] = []
    pagination_key: str | None = None

    for page in range(_V2_MAX_PAGES):
        params: dict = {
            "encoding": "jsonParsed",
            "filters": [
                {"dataSize": 165},
                {"memcmp": {"offset": 0, "bytes": contract_address}},
            ],
            "limit": _V2_PAGE_LIMIT,
        }
        if pagination_key:
            params["paginationKey"] = pagination_key

        payload = {
            "jsonrpc": "2.0",
            "id": "alphapulse-holder-data-v2",
            "method": "getProgramAccountsV2",
            "params": [TOKEN_PROGRAM_ID, params],
        }

        data = await helius_manager.request_json(
            "POST",
            "solana-json-rpc:getProgramAccountsV2",
            json_body=payload,
            priority=priority,
            timeout=20,
            context=f"holder_accounts_v2:{contract_address}:page{page}",
        )

        if data is None:
            if page == 0:
                # Helius not registered/configured, or unreachable — no V2
                # data at all. Let the caller fall back to the legacy chain.
                return None
            logger.warning(
                f"[HolderDiag] {contract_address[:8]}: getProgramAccountsV2 "
                f"page {page} got no response after {page} good page(s) — "
                f"returning the {len(all_accounts)} accounts already collected"
            )
            break

        if isinstance(data, dict) and data.get("error"):
            if page == 0:
                logger.info(
                    f"[HolderDiag] {contract_address[:8]}: getProgramAccountsV2 "
                    f"error on first page ({data['error']}) — falling back to "
                    f"legacy getProgramAccounts"
                )
                return None
            logger.warning(
                f"[HolderDiag] {contract_address[:8]}: getProgramAccountsV2 "
                f"page {page} error ({data['error']}) — returning the "
                f"{len(all_accounts)} accounts already collected"
            )
            break

        result = data.get("result") if isinstance(data, dict) else None
        page_accounts = result.get("accounts") if isinstance(result, dict) else None

        if page_accounts is None:
            # HTTP 200, error-free, but not the {"accounts": [...]} shape
            # V2 documents — an unexpected response we shouldn't trust.
            if page == 0:
                logger.warning(
                    f"[HolderDiag] {contract_address[:8]}: getProgramAccountsV2 "
                    f"returned an unexpected response shape on first page — "
                    f"falling back to legacy getProgramAccounts"
                )
                return None
            break

        if page == 0 and not page_accounts:
            # Ambiguous first-page empty result. Only Helius implements V2,
            # so there is no second provider to cross-validate against here
            # the way the legacy path can — defer to that path instead.
            return None

        all_accounts.extend(page_accounts)
        pagination_key = result.get("paginationKey")

        if not pagination_key or not page_accounts:
            break

        if len(all_accounts) >= MAX_HOLDER_ACCOUNTS * 4:
            # Already collected far more than balance-based truncation will
            # ever keep — stop paying for further pages that can't outrank
            # what's already in hand.
            logger.info(
                f"[HolderDiag] {contract_address[:8]}: getProgramAccountsV2 "
                f"collected {len(all_accounts)} accounts across {page + 1} "
                f"page(s), well past the {MAX_HOLDER_ACCOUNTS}-account cap — "
                f"stopping pagination early"
            )
            break
    else:
        logger.warning(
            f"[HolderDiag] {contract_address[:8]}: getProgramAccountsV2 hit "
            f"the {_V2_MAX_PAGES}-page safety cap ({len(all_accounts)} "
            f"accounts collected) — stopping; this token has an unusually "
            f"large holder set"
        )

    return all_accounts


async def _fetch_token_accounts(
    contract_address: str, priority: int = PRIORITY_LOW
) -> _HolderAccountsResult | None:
    """
    Shared fetch helper. Returns the parsed, deterministically-ordered
    holder account records for a mint (each with an "owner" and "amount"),
    or None if unavailable.

    Two-tier retrieval:

      1. Primary: Helius's proprietary `getProgramAccountsV2` (cursor-
         paginated, purpose-built for large-account-set programs like the
         SPL Token Program). See the comment above
         `_fetch_via_program_accounts_v2` for why this replaced plain
         `getProgramAccounts` as the first attempt — the short version is
         that a full-table scan of the Token Program via plain
         `getProgramAccounts` is a documented failure mode across
         providers (HTTP 200 with a silently empty/truncated result),
         which V2's pagination avoids.
      2. Fallback: the original standard Solana JSON-RPC `getProgramAccounts`
         method, routed through the shared MultiRPCManager's full
         Helius -> QuickNode -> Alchemy failover with cross-provider
         empty-result validation. Used whenever Helius/V2 is unreachable,
         errors, or returns an unconfirmed-empty first page — so this
         function still degrades gracefully even if Helius itself is down,
         instead of going dark the way a Helius-only implementation would.

    Caching: the parsed result is cached per contract for
    HELIUS_HOLDER_CACHE_TTL_SECONDS via providers.cache.get_cache() — the
    same Redis-backed-when-configured, in-memory-otherwise cache used by
    the market-data layer (providers/marketdata/_resilience.py). This is a
    deliberate change from the previous per-process
    multi_rpc_manager.get_cached()/set_cached(): this bot's production
    topology (Bible §3) runs the Signal/Trading worker and the
    Intelligence worker as two SEPARATE processes, each with its own
    in-memory multi_rpc_manager cache. Under the old cache, if both
    workers needed holder data for the same token within the same TTL
    window (a routine overlap — a signal alert firing while the
    Intelligence worker is scoring wallets against the same mint), each
    process fetched and cached independently: 2x the RPC calls, and a
    real chance the two workers scored the same token against two
    different holder snapshots within seconds of each other ("cache
    inconsistency" across workers). Sharing the cache (when REDIS_URL is
    configured; falls back to the old per-process behavior otherwise)
    fixes both — one fetch serves either worker, and both workers see the
    same snapshot for the TTL window.
    """
    cache = await get_cache()
    cache_key = _holder_cache_key(contract_address)
    try:
        cached = await cache.get(cache_key)
    except Exception:
        # Same principle as the cache.set() guard below: a cache-backend
        # hiccup (e.g. a transient Redis connection error) must never fail
        # the request outright — we just proceed as a cache miss and hit
        # the RPC fresh.
        logger.warning(f"[HolderDiag] {contract_address[:8]}: cache read failed, fetching fresh")
        cached = None
    if cached is not None:
        accounts = cached.get("accounts", [])
        logger.info(
            f"[HolderDiag] {contract_address[:8]}: served from shared cache "
            f"({len(accounts)} accounts) — no RPC request made"
        )
        return _HolderAccountsResult(
            accounts=accounts,
            truncated=cached.get("truncated", False),
            raw_account_count=cached.get("raw_account_count", len(accounts)),
        )

    logger.info(f"[HolderDiag] {contract_address[:8]}: holder accounts request started")

    # Primary path: Helius getProgramAccountsV2 — see the module-level
    # comment above _fetch_via_program_accounts_v2 for why this replaces
    # plain getProgramAccounts as the first attempt.
    raw_accounts = await _fetch_via_program_accounts_v2(contract_address, priority)
    used_v2 = raw_accounts is not None

    if raw_accounts is None:
        logger.info(
            f"[HolderDiag] {contract_address[:8]}: getProgramAccountsV2 path "
            f"unavailable/unconfirmed — falling back to legacy "
            f"getProgramAccounts across the full provider chain"
        )

        payload = {
            "jsonrpc": "2.0",
            "id": "alphapulse-holder-data",
            "method": "getProgramAccounts",
            "params": [
                TOKEN_PROGRAM_ID,
                {
                    "encoding": "jsonParsed",
                    # dataSize 165 = a legacy SPL Token account layout; memcmp
                    # at offset 0 (the account's mint field) narrows the scan to
                    # just this token's holder accounts.
                    "filters": [
                        {"dataSize": 165},
                        {"memcmp": {"offset": 0, "bytes": contract_address}},
                    ],
                },
            ],
        }

        # The URL argument is a label only — MultiRPCManager selects and builds
        # the actual per-provider endpoint internally based on the configured
        # failover order, not on anything passed here.
        #
        # exclude_providers=["drpc"]: Signal-path holder analysis is scoped to
        # Helius (primary) -> QuickNode -> Alchemy only, per the Signal
        # Accuracy/Quote Alert upgrade. This exclusion is local to this call
        # site — RPC_PROVIDER_PRIORITY and every other caller of
        # multi_rpc_manager (wallet balance, portfolio, etc.) are unaffected
        # and still get the full Helius -> QuickNode -> Alchemy -> dRPC chain.
        data = await helius_manager.request_json(
            "POST",
            "solana-json-rpc:getProgramAccounts",
            json_body=payload,
            priority=priority,
            timeout=20,
            context=f"holder_accounts:{contract_address}",
            exclude_providers=["drpc"],
            # See MultiRPCManager._dispatch: an HTTP-200/error-free response
            # with an empty "result" array is ambiguous for this call (a
            # genuinely brand-new mint vs. a provider plan/add-on that
            # silently can't run getProgramAccounts). Cross-validate against
            # the other eligible providers before trusting it, instead of
            # letting whichever provider happens to be tried first decide
            # "zero holders" for the whole request.
            retry_on_empty_result=True,
        )

        if data is None:
            logger.warning(
                f"[HolderDiag] {contract_address[:8]}: no response from any eligible "
                f"provider (all failed/exhausted) — holder accounts unavailable"
            )
            return None

        logger.info(f"[HolderDiag] {contract_address[:8]}: response received, parsing started")

        if data.get("error"):
            logger.warning(f"getProgramAccounts holder data error: {data['error']}")
            return None

        raw_accounts = data.get("result") or []
    else:
        logger.info(
            f"[HolderDiag] {contract_address[:8]}: getProgramAccountsV2 "
            f"returned {len(raw_accounts)} raw account entries, parsing started"
        )

    raw_account_count = len(raw_accounts)
    logger.info(
        f"[HolderDiag] {contract_address[:8]}: {raw_account_count} raw holder "
        f"account entries returned (source="
        f"{'getProgramAccountsV2' if used_v2 else 'getProgramAccounts-legacy'})"
    )
    # Only fresh RPC outcomes go into the rolling diagnostic window — a
    # cache hit is replaying an outcome already counted once, not new
    # evidence about current provider health.
    _record_holder_fetch_outcome(contract_address, was_empty=(raw_account_count == 0))

    # Parse ALL returned entries first, THEN truncate (if needed) by
    # sorted balance. This fixes the original truncation bug: slicing
    # `raw_accounts[:MAX_HOLDER_ACCOUNTS]` before parsing kept whatever
    # subset getProgramAccounts happened to return first. Solana RPC does
    # not guarantee getProgramAccounts response ordering is stable across
    # calls, so for any token over the cap that slice was effectively a
    # random sample each time — top_holder_pct/top10_pct/dev_holding_pct
    # could swing between calls (or the dev wallet could vanish from the
    # snapshot entirely) purely because of which arbitrary subset landed
    # in the first 5,000 entries, not because anything about the token
    # changed. Sorting by balance before truncating guarantees the wallets
    # that actually determine concentration/bundle/dev metrics are always
    # retained; only long-tail dust accounts (which don't move those
    # metrics) are ever dropped, and the same result is reproduced on
    # every call for the same underlying chain state.
    parsed_all: list[dict] = []
    for entry in raw_accounts:
        if not isinstance(entry, dict):
            continue
        account = entry.get("account") or {}
        parsed_data = account.get("data")
        parsed_info = (parsed_data.get("parsed") or {}).get("info") if isinstance(parsed_data, dict) else None
        if not parsed_info:
            continue

        owner = parsed_info.get("owner")
        token_amount = parsed_info.get("tokenAmount") or {}
        amount = token_amount.get("amount")  # raw base-unit string; ratios below are self-consistent regardless of decimals

        if not owner or amount is None:
            continue

        try:
            amount_sort_key = int(amount)
        except (ValueError, TypeError):
            amount_sort_key = 0

        parsed_all.append({"owner": owner, "amount": amount, "_sort": amount_sort_key})

    truncated = len(parsed_all) > MAX_HOLDER_ACCOUNTS
    if truncated:
        # Secondary key on owner gives deterministic tie-breaking when two
        # accounts report identical amounts, instead of depending on
        # whatever order the RPC happened to return them in.
        parsed_all.sort(key=lambda a: (-a["_sort"], a["owner"]))
        logger.info(
            f"Holder account list for {contract_address[:8]} truncated: "
            f"{len(parsed_all)} -> {MAX_HOLDER_ACCOUNTS} (kept largest balances)"
        )
        parsed_all = parsed_all[:MAX_HOLDER_ACCOUNTS]

    accounts_all = [{"owner": a["owner"], "amount": a["amount"]} for a in parsed_all]

    logger.info(
        f"[HolderDiag] {contract_address[:8]}: parsing completed, "
        f"{len(accounts_all)} valid holder accounts extracted"
        + (" (truncated to largest balances)" if truncated else "")
    )

    if raw_account_count > 0 and not accounts_all:
        # The RPC returned raw entries but every single one was dropped
        # during parsing (missing "owner"/"tokenAmount", unexpected shape,
        # etc.) — this is a PARSER problem, not "this token has no
        # holders", and is worth calling out distinctly from the
        # legitimate-early-token empty case so it doesn't get silently
        # mistaken for one.
        logger.warning(
            f"[HolderDiag] {contract_address[:8]}: parser dropped ALL "
            f"{raw_account_count} raw holder account entries (0 survived "
            f"parsing) — this looks like a response-shape/parsing bug, not "
            f"a genuinely empty token. Investigate the raw entry shape for "
            f"this mint if this recurs."
        )

    # Cache successful (non-empty) holder snapshots for the normal, longer
    # TTL. Cache zero-holder results — whether from raw_account_count == 0
    # (genuinely/cross-validated empty) or from the parser-drop case above
    # — for a much shorter TTL instead. A stale "0 accounts" answer sitting
    # in the shared cache for the full multi-minute holder-snapshot window
    # is exactly what was poisoning repeat lookups ("served from shared
    # cache (0 accounts)" on every re-check of the same mint); a short TTL
    # here lets the cache self-heal on the next re-check instead.
    cache_ttl = HELIUS_HOLDER_CACHE_TTL_SECONDS if accounts_all else HELIUS_HOLDER_EMPTY_CACHE_TTL_SECONDS

    cache_payload = {
        "accounts": accounts_all,
        "truncated": truncated,
        "raw_account_count": raw_account_count,
    }
    try:
        await cache.set(cache_key, cache_payload, cache_ttl)
    except Exception:
        # Cache write failures must never fail the request — we already
        # have the data to return, we just won't share/reuse it this time.
        logger.warning(f"[HolderDiag] {contract_address[:8]}: cache write failed, continuing uncached")

    return _HolderAccountsResult(
        accounts=accounts_all, truncated=truncated, raw_account_count=raw_account_count
    )


async def get_holder_count(contract_address: str, priority: int = PRIORITY_LOW) -> int | None:
    """
    Get token holder count via standard Solana RPC (getProgramAccounts).

    Returns None if unavailable (all configured providers failed/unreachable).
    Note: if the holder set exceeds MAX_HOLDER_ACCOUNTS, this reflects the
    (deterministic, largest-balance-first) truncated set, i.e. a lower
    bound on true holder count for very large-holder tokens — see
    get_holder_analysis()'s holder_data_truncated flag for the same
    caveat with more detail.
    """
    result = await _fetch_token_accounts(contract_address, priority=priority)
    if result is None:
        return None
    return len({a.get("owner") or a.get("address") for a in result.accounts})


def _cluster_bundles(balances: list[tuple[str, float]], total_supply_seen: float) -> tuple[int, float]:
    """
    Groups wallets with near-identical holdings into candidate "bundle"
    clusters (same-size buys/allocations are the strongest surface signal
    of coordinated wallets without needing full funding-graph tracing).

    Wallets below MIN_BUNDLE_WALLET_SUPPLY_PCT of supply are excluded from
    the clustering input (though still counted in total_holders elsewhere)
    — see the constant's docstring for why: dust-level coincidental
    similarity between unrelated small wallets was the main source of
    false-positive Sybil/bundle flags.

    Returns (wallet_count_in_bundles, combined_supply_fraction_pct). The
    percentage is always computed against the FULL total_supply_seen
    (including dust wallets), so it still represents a true share of
    tracked supply.
    """
    if not balances or total_supply_seen <= 0:
        return 0, 0.0

    dust_floor = total_supply_seen * (MIN_BUNDLE_WALLET_SUPPLY_PCT / 100)
    material = [(owner, amt) for owner, amt in balances if amt > dust_floor]
    if not material:
        return 0, 0.0

    # Deterministic ordering: amount descending, owner ascending as a
    # tie-break. Without the tie-break, wallets with identical balances
    # (common with round-number/default buy amounts) sort in whatever
    # order the underlying dict happened to iterate them, which traces
    # back to non-deterministic RPC response order — meaning the exact
    # set of wallets that end up clustered together (and therefore
    # bundle_pct itself) could vary between otherwise-identical calls.
    sorted_balances = sorted(material, key=lambda x: (-x[1], x[0]))
    used = [False] * len(sorted_balances)
    bundled_wallets = 0
    bundled_amount = 0.0

    for i, (_, amt_i) in enumerate(sorted_balances):
        if used[i] or amt_i <= 0:
            continue
        cluster = [i]
        for j in range(i + 1, len(sorted_balances)):
            if used[j]:
                continue
            _, amt_j = sorted_balances[j]
            if amt_j <= 0:
                continue
            if abs(amt_i - amt_j) / amt_i <= BUNDLE_BALANCE_TOLERANCE:
                cluster.append(j)

        if len(cluster) >= MIN_BUNDLE_CLUSTER_SIZE:
            for idx in cluster:
                used[idx] = True
                bundled_wallets += 1
                bundled_amount += sorted_balances[idx][1]

    return bundled_wallets, round((bundled_amount / total_supply_seen) * 100, 2)


async def _resolve_bundle_risk(
    contract_address: str,
    balances: list[tuple[str, float]],
    total_supply_seen: float,
) -> tuple[int, float, str]:
    """
    Bundle-risk source selection.

    Solana Tracker's authoritative risk.bundlers.totalPercentage
    (providers.marketdata.solanatracker.get_bundle_risk_pct()) is now the
    PRIMARY source for bundle_pct: it comes from Solana Tracker's own
    provider-side bundler detection rather than this module's own
    +/-15% balance-similarity heuristic (BUNDLE_BALANCE_TOLERANCE /
    _cluster_bundles() above), so it isn't subject to the same
    coincidental-round-buy-size false positives that heuristic's
    docstring describes.

    _cluster_bundles() is preserved UNCHANGED and used ONLY as a safe
    fallback -- when BUNDLE_RISK_SOLANA_TRACKER_ENABLED is off,
    SOLANA_TRACKER_API_KEY isn't configured, or the Solana Tracker
    lookup itself fails / raises / returns no bundlers data. A genuine
    "0% bundled" reading from Solana Tracker is a real result and is
    used as-is, never treated as "unavailable".

    Nothing downstream changes: domain.signals.scoring.BUNDLE_SEVERE_PCT
    (the 70% hard-reject gate), BUNDLE_MODERATE_PCT,
    evaluate_sybil_bundle_risk(), hard_reject_reasons(), and
    _score_holder_distribution() all keep consuming bundle_pct exactly
    as before -- they have no idea which source produced it.

    Returns (bundle_wallet_count, bundle_pct, source) where source is
    "solana_tracker" or "balance_similarity_fallback" (informational
    only -- not consumed by scoring/qualification).
    """
    if BUNDLE_RISK_SOLANA_TRACKER_ENABLED:
        try:
            st_pct, st_count = await get_bundle_risk_pct(contract_address)
        except Exception:
            logger.warning(
                f"[HolderDiag] {contract_address[:8]}: Solana Tracker bundler "
                "lookup raised - falling back to balance-similarity clustering",
                exc_info=True,
            )
            st_pct, st_count = None, None

        if st_pct is not None:
            return (st_count if st_count is not None else 0), st_pct, "solana_tracker"

    bundle_wallets, bundle_pct = _cluster_bundles(balances, total_supply_seen)
    return bundle_wallets, bundle_pct, "balance_similarity_fallback"


async def get_holder_analysis(
    contract_address: str,
    dev_address: str | None = None,
    priority: int = PRIORITY_LOW,
    is_pump_fun: bool = False,
) -> dict | None:
    """
    Full holder-distribution snapshot for signal alerts:
      - total_holders
      - top_holder_pct / top10_pct / top25_pct  (share of tracked supply)
      - dev_holding_pct  (only populated if dev_address is known, e.g. from
        GoPlus's creator_address)
      - bundle_wallet_count / bundle_pct  (heuristic cluster detection —
        see BUNDLE_BALANCE_TOLERANCE note above)
      - holder_analysis_status  ("ok" on a normal successful analysis, or
        "unavailable_early_token" — see below)

    Returns None only when the RPC call itself failed (timeout, HTTP error,
    exception, or every configured provider exhausted) — caller should
    treat this as a genuine provider failure and fall back to GoPlus's
    top_holder_percent / top_10_holder_percent instead.

    Distinct from that: the RPC can succeed (HTTP 200, no error) and still
    return zero holder accounts. Pump.fun's very early tokens routinely hit
    this — the getProgramAccounts-based method this module uses often
    doesn't expose holder accounts yet for brand-new bonding-curve mints.
    That is NOT a provider failure, so for Pump.fun tokens (is_pump_fun=True)
    it is reported as a distinct dict with
    holder_analysis_status="unavailable_early_token" instead of None, so the
    caller can continue scoring on liquidity/market cap/volume/buy-sell
    pressure/security checks rather than hard-rejecting on missing data.
    Non-Pump.fun callers keep the original behavior (None) since this early-
    token gap is specific to freshly-launched Pump.fun mints.
    """
    fetch_result = await _fetch_token_accounts(contract_address, priority=priority)

    if fetch_result is None:
        # RPC-level failure — timeout, HTTP error, exception, or all
        # eligible providers exhausted. Keep the existing fail-closed
        # behavior: this is a genuine "we don't know" state.
        logger.warning(
            f"[HolderDiag] {contract_address[:8]}: no holder accounts available — "
            f"returning None to scoring engine (caller will treat as unavailable)"
        )
        return None

    accounts = fetch_result.accounts

    if not accounts:
        # RPC succeeded (HTTP 200, no error) but returned zero raw holder
        # account entries. For Pump.fun tokens this is a known early-token
        # gap, not a failure — surface it as a distinct state instead of
        # forcing a hard reject.
        if is_pump_fun:
            logger.info(
                f"[HolderDiag] Pump.fun early token {contract_address[:8]} - "
                "holder accounts unavailable, continuing with partial scoring"
            )
            return {
                "total_holders": None,
                "top_holder_pct": None,
                "top10_pct": None,
                "top25_pct": None,
                "dev_holding_pct": None,
                "bundle_wallet_count": None,
                "bundle_pct": None,
                "top_holder_addresses": [],
                "holder_analysis_status": "unavailable_early_token",
                "holder_data_truncated": False,
            }
        logger.warning(
            f"[HolderDiag] {contract_address[:8]}: RPC succeeded but returned zero "
            f"holder accounts — returning None to scoring engine (caller will treat as unavailable)"
        )
        return None

    raw_balances: list[tuple[str, float]] = []
    for acc in accounts:
        owner = acc.get("owner") or acc.get("address") or ""
        try:
            amount = float(acc.get("amount") or 0)
        except (ValueError, TypeError):
            amount = 0.0
        if amount > 0:
            raw_balances.append((owner, amount))

    if not raw_balances:
        return {
            "total_holders": len(accounts),
            "top_holder_pct": None,
            "top10_pct": None,
            "top25_pct": None,
            "dev_holding_pct": None,
            "bundle_wallet_count": None,
            "bundle_pct": None,
            "top_holder_addresses": [],
            "holder_analysis_status": "ok",
            "holder_data_truncated": fetch_result.truncated,
        }

    # Some tokens split a single owner across multiple associated token
    # accounts — merge by owner so "top holder" reflects real wallets.
    by_owner: dict[str, float] = defaultdict(float)
    for owner, amt in raw_balances:
        by_owner[owner] += amt

    total_supply_seen = sum(by_owner.values()) or 1.0
    # Secondary sort key (owner ascending) makes tie-breaking deterministic
    # instead of depending on dict insertion order, which itself traces
    # back to non-deterministic RPC response ordering — without this, two
    # otherwise-identical calls could expose top_holder_addresses (and the
    # top1/top10/top25 boundary in the rare exact-tie case) in a different
    # order, which downstream whale/KOL cross-referencing treats as a
    # different snapshot.
    sorted_owners = sorted(by_owner.items(), key=lambda x: (-x[1], x[0]))

    def _share(n: int) -> float:
        return round(sum(v for _, v in sorted_owners[:n]) / total_supply_seen * 100, 2)

    top_holder_pct = _share(1)
    top10_pct = _share(10)
    top25_pct = _share(25)

    # NOTE: if the dev wallet isn't in by_owner at all (i.e. it currently
    # holds zero tokens, e.g. it sold its full allocation), that's a real
    # and meaningful 0% — not missing data. Only leave this as None when we
    # genuinely don't know the dev wallet's address, so alerts never show a
    # misleading "N/A" for a dev that has actually cashed out.
    dev_pct = round(by_owner.get(dev_address, 0.0) / total_supply_seen * 100, 2) if dev_address else None

    bundle_wallets, bundle_pct, bundle_data_source = await _resolve_bundle_risk(
        contract_address, list(by_owner.items()), total_supply_seen
    )

    result = {
        "total_holders": len(by_owner),
        "top_holder_pct": top_holder_pct,
        "top10_pct": top10_pct,
        "top25_pct": top25_pct,
        "dev_holding_pct": dev_pct,
        "bundle_wallet_count": bundle_wallets,
        "bundle_pct": bundle_pct,
        "bundle_data_source": bundle_data_source,
        "top_holder_addresses": [owner for owner, _ in sorted_owners[:MAX_HOLDER_ADDRESSES_EXPOSED]],
        "holder_analysis_status": "ok",
        # True when the raw holder set exceeded MAX_HOLDER_ACCOUNTS and had
        # to be truncated to the largest balances (see _fetch_token_accounts).
        # top_holder_pct/top10_pct/top25_pct/dev_holding_pct/bundle_* remain
        # accurate even when this is True (truncation only ever drops small
        # long-tail wallets); total_holders becomes a lower bound. Downstream
        # consumers that want to discount confidence on very large, heavily
        # truncated holder sets can key off this flag.
        "holder_data_truncated": fetch_result.truncated,
    }
    logger.info(
        f"[HolderDiag] {contract_address[:8]}: HolderAnalysis created "
        f"(total_holders={result['total_holders']}, top_holder_pct={result['top_holder_pct']}) "
        f"— returning to scoring engine"
    )
    return result
