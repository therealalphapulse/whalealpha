import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Single Telegram user ID for the bot Owner (full RBAC access, cannot be
# removed/demoted — see services/admin_rbac.py). Unset by default; the
# owner bootstrap is skipped with a warning until this is configured.
_raw_owner_id = os.getenv("OWNER_ID", "").strip()
OWNER_ID: int | None = None
if _raw_owner_id:
    try:
        OWNER_ID = int(_raw_owner_id)
    except ValueError:
        OWNER_ID = None

# Comma-separated Telegram user IDs allowed to run Premium admin commands
# (/premium_grant, /premium_revoke, /premium_extend, /premium_list). Empty
# by default — no one has admin access until this is configured.
_raw_admin_ids = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS: set[int] = set()
if _raw_admin_ids:
    for _part in _raw_admin_ids.split(","):
        _part = _part.strip()
        if _part:
            try:
                ADMIN_IDS.add(int(_part))
            except ValueError:
                pass

# API Keys — Multi-RPC Provider Support
# The MultiRPCManager uses these in priority order. Configure any combination;
# unconfigured providers are skipped automatically.
#
# Values are stripped so a Railway/host variable that's set to whitespace-only
# (which is truthy in Python, e.g. " ") is correctly treated as "not
# configured" instead of silently registering a provider with a blank key.
def _env_key(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


HELIUS_API_KEY = _env_key("HELIUS_API_KEY")
ALCHEMY_API_KEY = _env_key("ALCHEMY_API_KEY")
DRPC_API_KEY = _env_key("DRPC_API_KEY")
QUICKNODE_API_KEY = _env_key("QUICKNODE_API_KEY")
# Additional RPC/failover provider (AlphaPulse Provider Integration Task,
# 2026-08-19). Purely additive: only takes effect if ANKR_API_KEY is set,
# and only ever runs AFTER the existing Helius/QuickNode/Alchemy/dRPC chain
# in RPC_PROVIDER_PRIORITY below (appended at the end, not inserted).
ANKR_API_KEY = _env_key("ANKR_API_KEY")

# Optional. Jupiter's free, no-key tier (lite-api.jup.ag) is used for Real
# Wallet swaps if this is unset. Set it to use Jupiter's api.jup.ag tier
# instead, which has higher rate limits — see services/jupiter_swap.py.
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY")

# API Base URLs (all free)
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"
# Root API host for DexScreener endpoints that live outside /latest/dex
# (token-profiles/latest, token-boosts/latest) — used by the v4 discovery
# adapter's profile-required candidate feed.
DEXSCREENER_ROOT_API = "https://api.dexscreener.com"
GECKOTERMINAL_API = "https://api.geckoterminal.com/api/v2"
COINGECKO_API = "https://api.coingecko.com/api/v3"
GOPLUS_API = "https://api.gopluslabs.io/api/v1"
HELIUS_API = "https://api.helius.xyz/v0"
# RugCheck (AlphaPulse Provider Integration Task, 2026-08-19). Purely
# additive token-security fallback -- only takes effect if
# RUGCHECK_API_KEY is set, and only ever runs after GoPlus's own two
# endpoint attempts in check_token_security() both return no usable
# data. See providers/marketdata/rugcheck.py.
RUGCHECK_API = "https://api.rugcheck.xyz/v1"
RUGCHECK_API_KEY = _env_key("RUGCHECK_API_KEY")

# KOL Provider Sync (optional — bot works fine without these)
KOL_PROVIDER_URL = os.getenv("KOL_PROVIDER_URL")
KOL_PROVIDER_API_KEY = os.getenv("KOL_PROVIDER_API_KEY")
KOL_PROVIDER_FORMAT = os.getenv("KOL_PROVIDER_FORMAT", "json").lower()
KOL_PROVIDER_NAME = os.getenv("KOL_PROVIDER_NAME", "kol_provider")

try:
    KOL_SYNC_INTERVAL_SECONDS = int(os.getenv("KOL_SYNC_INTERVAL_SECONDS", "300"))
except ValueError:
    KOL_SYNC_INTERVAL_SECONDS = 300

# How long to wait after the 15th new signal in a batch before sending the
# Performance Recap, so the freshest signal in the batch has a moment to
# start moving before it's scored (configurable via env var).
try:
    PERFORMANCE_RECAP_DELAY_SECONDS = int(os.getenv("PERFORMANCE_RECAP_DELAY_SECONDS", "300"))
except ValueError:
    PERFORMANCE_RECAP_DELAY_SECONDS = 300

# Bot Settings
BOT_NAME = "AlphaPulse"
BOT_VERSION = "3.3.0"
SUPPORTED_CHAIN = "solana"

# PnL Card mascot (dynamic/customizable — see services/pnl_image.py).
# PNL_MASCOT_DIR: a folder of PNG/JPG/WEBP images; one is picked at random
#   per card so the mascot can vary/rotate without a redeploy.
# PNL_MASCOT_PATH: a single fixed image, used if PNL_MASCOT_DIR isn't set.
# If neither is set (or the folder is empty), one of several built-in
# drawn faces (shocked/excited/happy/neutral/worried/crying, chosen at
# random per outcome tier) is used as a safe fallback.
PNL_MASCOT_DIR = os.getenv("PNL_MASCOT_DIR")
PNL_MASCOT_PATH = os.getenv("PNL_MASCOT_PATH")


# ============================================================
# Premium Intelligence Engine (Smart Wallet Discovery / Scoring /
# Consensus Signals) — all tunable via env vars, sane defaults so the
# engine runs autonomously out of the box with no configuration.
# ============================================================

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return list(default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


# ============================================================
# Multi-RPC Request Manager (services/multi_rpc_manager.py)
#
# Every RPC/API-backed service (holder analysis, wallet portfolio/balance,
# signal scanner, watchlist, smart wallet intelligence, funding-graph
# tracing, deployer history, address resolution) shares ONE queue/limiter
# that automatically fails over across configured providers (Helius → Alchemy
# → dRPC → QuickNode). Sane conservative defaults so the bot stays inside a
# free-tier plan out of the box; raise MAX_REQUESTS_PER_SECOND if on a paid
# plan or multi-key setup.
# ============================================================

# Global ceiling on outbound RPC requests/sec across the ENTIRE bot
# (all background loops + user-triggered commands combined). Kept
# conservative by default since free-tier plans throttle well below
# their advertised burst rate under sustained/concurrent load.
# Note: this is per-provider, so with 4 providers configured, effective
# ceiling is ~8 req/s across the failover pool.
MULTI_RPC_MAX_REQUESTS_PER_SECOND = _env_float("MULTI_RPC_MAX_REQUESTS_PER_SECOND", 2.0)

# How many times a rate-limited/timed-out/transient-failure request is
# retried (with exponential backoff) before giving up and returning None
# (unknown, not zero — every caller already treats None this way).
MULTI_RPC_MAX_RETRIES = _env_int("MULTI_RPC_MAX_RETRIES", 4)

# Per-request network timeout (seconds) applied to every provider call
# unless a caller passes an explicit `timeout=` to request_json().
MULTI_RPC_TIMEOUT_SECONDS = _env_int("MULTI_RPC_TIMEOUT_SECONDS", 15)

# Ceiling on requests in flight at once across all providers combined.
# 0 (or negative) means "no cap" — bounded only by MAX_REQUESTS_PER_SECOND.
MULTI_RPC_MAX_CONCURRENT_REQUESTS = _env_int("MULTI_RPC_MAX_CONCURRENT_REQUESTS", 10)

# Window during which two identical in-flight requests (same method + url +
# body) are collapsed into a single upstream call, with both callers
# receiving the same result.
MULTI_RPC_DEDUP_WINDOW_SECONDS = _env_float("MULTI_RPC_DEDUP_WINDOW_SECONDS", 2.0)

# How long a circuit-broken provider is skipped before a recovery attempt is
# allowed. Falls back to MULTI_RPC_HEALTH_CHECK_INTERVAL_SECONDS (below) when
# unset/zero, since the two are conceptually the same "try again after N
# seconds" cooldown.
MULTI_RPC_PROVIDER_COOLDOWN_SECONDS = _env_int("MULTI_RPC_PROVIDER_COOLDOWN_SECONDS", 60)

# Per-provider on/off switches. Each provider is used only if it is enabled
# here AND it has a usable endpoint configured — unset keys are skipped
# automatically regardless of this flag. Defaults to enabled so a provider
# only needs its credentials set to start being used.
ENABLE_HELIUS = _env_bool("ENABLE_HELIUS", True)
ENABLE_ALCHEMY = _env_bool("ENABLE_ALCHEMY", True)
ENABLE_DRPC = _env_bool("ENABLE_DRPC", True)
ENABLE_QUICKNODE = _env_bool("ENABLE_QUICKNODE", True)
ENABLE_ANKR = _env_bool("ENABLE_ANKR", True)

# QuickNode Solana RPC endpoint. QuickNode issues a full per-account URL
# (e.g. https://your-name.solana-mainnet.quiknode.pro/TOKEN/) rather than a
# generic host + appended API key like Alchemy/Helius, so it's configured as
# one complete URL instead of a bare key. QUICKNODE_API_KEY is still read as
# a legacy fallback (appended to a generic QuickNode host) for anyone who
# configured it the old way, but QUICKNODE_SOLANA_RPC takes priority.
#
# CONFIRMED BUG, FIXED HERE (Production Recovery Audit): .env.example
# documented this variable as QUICKNODE_RPC_URL, a name this file never
# actually read. Any deployment that followed .env.example literally would
# have QUICKNODE_RPC_URL set in its real environment but QUICKNODE_SOLANA_RPC
# would still evaluate to None — silently dropping QuickNode out of
# _init_providers() (falsy api_key => provider skipped, logged only at
# INFO level, not an error) with ZERO functional impact on
# RPC_PROVIDER_PRIORITY (it would just never resolve to a registered
# provider). This is significant specifically because QuickNode is placed
# FIRST in the default failover order right after Helius (see
# RPC_PROVIDER_PRIORITY below) — "the most reliable first fallback in
# production" per the comment that was already there. Losing it silently
# would leave holder-account scans (which additionally exclude dRPC — see
# domain/intelligence/holders.py) effectively down to Helius alone whenever
# Alchemy also isn't configured, which is exactly the failure mode
# retry_on_empty_result's cross-validation cannot help with (nothing to
# cross-validate against with only one eligible provider — see that
# function's own "limited cross-validation" warning).
#
# Fixed by accepting BOTH names: QUICKNODE_SOLANA_RPC (the name every other
# reference to this setting in this codebase already uses) takes priority
# if both happen to be set; QUICKNODE_RPC_URL (the name .env.example
# documented) is read as a fallback so an existing deployment that already
# followed the docs starts working without needing to rename anything.
# .env.example itself is also corrected (see that file) so new deployments
# aren't steered wrong going forward.
QUICKNODE_SOLANA_RPC = _env_key("QUICKNODE_SOLANA_RPC") or _env_key("QUICKNODE_RPC_URL")

# Failover order. Must use the provider keys the manager knows about:
# helius, alchemy, drpc, quicknode. Unknown entries are skipped with a
# warning at startup; unconfigured/disabled providers in the list are
# skipped too.
#
# Default order is Helius (primary) -> QuickNode -> Alchemy -> dRPC. Helius
# free/starter tiers rate-limit (HTTP 429) under sustained scan-cycle load,
# and QuickNode's Solana RPC has proven the most reliable first fallback in
# production, so it's placed immediately after Helius ahead of Alchemy/dRPC.
# NOTE (AlphaPulse Provider Integration Task, 2026-08-19): "ankr" was
# appended at the END of the default failover order below. This is
# additive only -- the existing Helius -> QuickNode -> Alchemy -> dRPC
# order and behavior are unchanged; Ankr is only ever tried after all
# four of them are unavailable/circuit-broken, and only if ANKR_API_KEY
# is actually configured (see _init_providers() in multi_rpc_manager.py --
# an unconfigured provider is skipped regardless of its position here).
RPC_PROVIDER_PRIORITY = _env_list(
    "RPC_PROVIDER_PRIORITY", ["helius", "quicknode", "alchemy", "drpc", "ankr"]
)

# Default TTL for the shared RPC response cache. Individual call sites
# may pass a shorter/longer TTL where appropriate, but this is the fallback.
MULTI_RPC_CACHE_TTL_SECONDS = _env_float("MULTI_RPC_CACHE_TTL_SECONDS", 60.0)

# Cache lifetime for full holder-distribution snapshots (services/holders.py).
# Holder makeup doesn't meaningfully change second-to-second, so re-scans of
# the same candidate within a cycle reuse this instead of re-fetching.
# 7 minutes — inside the requested 5-10 minute window; the single biggest
# lever for cutting duplicate Helius/RPC traffic since every candidate is
# revisited multiple times per scan cycle.
MULTI_RPC_HOLDER_CACHE_TTL_SECONDS = _env_float("MULTI_RPC_HOLDER_CACHE_TTL_SECONDS", 420.0)

# Cache lifetime specifically for ZERO-holder results (services/holders.py).
# Deliberately much shorter than MULTI_RPC_HOLDER_CACHE_TTL_SECONDS. A
# "0 accounts" result — even after cross-provider validation in
# MultiRPCManager (see retry_on_empty_result) — is either a genuinely
# brand-new Pump.fun mint (which can gain real holders within seconds as
# the bonding curve gets bought into) or a residual provider quirk that
# cross-validation didn't fully rule out. Caching it for the full 7-minute
# holder-snapshot TTL would let one stale "0 accounts" answer poison every
# re-check of that mint for the rest of the scan cycle ("shared cache
# permanently returning 0 accounts"). A short TTL here means a bad/early
# empty result self-heals on its own within seconds instead of requiring a
# cache flush, while still preventing a re-check flood on a token that's
# being repeatedly re-scored in a tight loop.
MULTI_RPC_HOLDER_EMPTY_CACHE_TTL_SECONDS = _env_float(
    "MULTI_RPC_HOLDER_EMPTY_CACHE_TTL_SECONDS", 20.0
)

# Cache lifetime for wallet portfolio/asset snapshots (services/wallet_
# portfolio.py, services/wallet_intelligence.py). Short, since this feeds
# user-facing balance displays that should reflect a fresh(ish) balance.
MULTI_RPC_WALLET_CACHE_TTL_SECONDS = _env_float("MULTI_RPC_WALLET_CACHE_TTL_SECONDS", 30.0)

# Cache lifetime for token metadata lookups (services/solana_resolver.py:
# get_asset_metadata — name/symbol/decimals/supply). Metadata is effectively
# static once a token is deployed, so this can be cached longer than price
# data without going stale.
MULTI_RPC_METADATA_CACHE_TTL_SECONDS = _env_float("MULTI_RPC_METADATA_CACHE_TTL_SECONDS", 600.0)

# Cache lifetime for funding-graph / deployer-history lookups
# (services/funding_graph.py, services/deployer_history.py). These trace a
# wallet's full tx history, so results are cached longer.
MULTI_RPC_HISTORY_CACHE_TTL_SECONDS = _env_float("MULTI_RPC_HISTORY_CACHE_TTL_SECONDS", 600.0)

# Provider health check: how often to attempt recovery from a circuit-broken
# provider (seconds). Once a provider fails, we try a fresh request every N
# seconds to see if it's back online before we consider it healthy again.
MULTI_RPC_HEALTH_CHECK_INTERVAL_SECONDS = _env_int("MULTI_RPC_HEALTH_CHECK_INTERVAL_SECONDS", 30)

# Circuit breaker: after how many consecutive failures should a provider be
# marked unhealthy and temporarily skipped? Retries to that provider resume
# after HEALTH_CHECK_INTERVAL has passed.
MULTI_RPC_CIRCUIT_BREAKER_THRESHOLD = _env_int("MULTI_RPC_CIRCUIT_BREAKER_THRESHOLD", 5)

# Backward-compatible aliases: services/holders.py, services/wallet_portfolio.py,
# services/wallet_intelligence.py, services/helius.py, services/funding_graph.py,
# and services/deployer_history.py were written against the pre-refactor
# HELIUS_*_CACHE_TTL_SECONDS names. Keep both names pointing at the same
# env-configurable value rather than touching every call site.
HELIUS_HOLDER_CACHE_TTL_SECONDS = MULTI_RPC_HOLDER_CACHE_TTL_SECONDS
HELIUS_HOLDER_EMPTY_CACHE_TTL_SECONDS = MULTI_RPC_HOLDER_EMPTY_CACHE_TTL_SECONDS
HELIUS_WALLET_CACHE_TTL_SECONDS = MULTI_RPC_WALLET_CACHE_TTL_SECONDS
HELIUS_HISTORY_CACHE_TTL_SECONDS = MULTI_RPC_HISTORY_CACHE_TTL_SECONDS


# --- DIAGNOSTIC TESTING FLAG (temporary) ------------------------------
# Master on/off switch for the Premium background schedulers only:
#   - Premium Intelligence Engine (services/premium_service.py::start_premium_intelligence_engine)
#       -> which in turn starts:
#          Smart Wallet Discovery Engine, Wallet Intelligence Scoring Engine,
#          Wallet Maintenance Engine, Wallet Monitor / Signal generation loop
#   - Premium expiry sweep scheduler (services/premium_service.py::premium_expiry_sweep_loop)
#
# Defaults to False (disabled) for diagnostic testing. Set
# PREMIUM_BACKGROUND_SCHEDULERS_ENABLED=true in the environment to
# re-enable — no code changes needed to turn it back on. This flag does
# NOT touch the Telegram bot, Signal Alert System, Real Wallet, Paper
# Trading, DCA, Trade Automation, Limit Orders, Exit Engine, database,
# or user commands — those all start unconditionally as before.
PREMIUM_BACKGROUND_SCHEDULERS_ENABLED = os.getenv(
    "PREMIUM_BACKGROUND_SCHEDULERS_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
# ------------------------------------------------------------------------

# Smart Wallet database sizing (Discovery / Maintenance)
#
# Operational Threshold model (see services/premium_signal_engine.py and
# services/premium_wallet_discovery.py):
#   Minimum Operational Threshold -> PREMIUM_WALLET_INITIAL_TARGET
#     (engine may begin generating Premium Intelligence once the active
#     wallet count reaches this many)
#   Recommended Capacity          -> PREMIUM_WALLET_LONGTERM_TARGET
#     (Premium confidence improves as the DB approaches this size)
#   Optimal Capacity              -> PREMIUM_WALLET_HARD_CAP
#     (highest-quality Premium consensus; also the discovery/trim ceiling)
PREMIUM_WALLET_INITIAL_TARGET = _env_int("PREMIUM_WALLET_INITIAL_TARGET", 500)
PREMIUM_WALLET_LONGTERM_TARGET = _env_int("PREMIUM_WALLET_LONGTERM_TARGET", 1000)
PREMIUM_WALLET_HARD_CAP = _env_int("PREMIUM_WALLET_HARD_CAP", 1500)

# How often each background loop runs
PREMIUM_DISCOVERY_INTERVAL_SECONDS = _env_int("PREMIUM_DISCOVERY_INTERVAL_SECONDS", 1800)
# Bootstrap Discovery: while the active wallet count is still below the
# Minimum Operational Threshold, discovery cycles this often instead —
# populating the database as fast as the underlying APIs comfortably
# allow. Once the threshold is reached, the loop settles back to the
# steady-state PREMIUM_DISCOVERY_INTERVAL_SECONDS cadence automatically,
# with no manual phase switch required.
PREMIUM_BOOTSTRAP_DISCOVERY_INTERVAL_SECONDS = _env_int("PREMIUM_BOOTSTRAP_DISCOVERY_INTERVAL_SECONDS", 300)
PREMIUM_MONITOR_INTERVAL_SECONDS = _env_int("PREMIUM_MONITOR_INTERVAL_SECONDS", 90)
PREMIUM_SCORING_INTERVAL_SECONDS = _env_int("PREMIUM_SCORING_INTERVAL_SECONDS", 900)
PREMIUM_MAINTENANCE_INTERVAL_SECONDS = _env_int("PREMIUM_MAINTENANCE_INTERVAL_SECONDS", 3600)

# How many wallets get their on-chain activity re-checked per monitor
# cycle (round-robin, elite tier checked more frequently) — keeps API
# usage bounded regardless of how large the wallet DB grows.
PREMIUM_MONITOR_BATCH_SIZE = _env_int("PREMIUM_MONITOR_BATCH_SIZE", 40)
PREMIUM_DISCOVERY_BATCH_SIZE = _env_int("PREMIUM_DISCOVERY_BATCH_SIZE", 60)

# Consensus thresholds — a token needs BOTH gates to become a Premium
# Signal (see services/premium_signal_engine.py).
PREMIUM_CONSENSUS_MIN_WALLETS = _env_int("PREMIUM_CONSENSUS_MIN_WALLETS", 3)
PREMIUM_CONSENSUS_WINDOW_MINUTES = _env_float("PREMIUM_CONSENSUS_WINDOW_MINUTES", 45)
PREMIUM_CONSENSUS_MIN_AI_SCORE = _env_float("PREMIUM_CONSENSUS_MIN_AI_SCORE", 65.0)
PREMIUM_CONSENSUS_MIN_AVG_REPUTATION = _env_float("PREMIUM_CONSENSUS_MIN_AVG_REPUTATION", 55.0)

# Wallet lifecycle thresholds
PREMIUM_WALLET_MIN_ACTIVATE_SCORE = _env_float("PREMIUM_WALLET_MIN_ACTIVATE_SCORE", 45.0)
PREMIUM_WALLET_ELITE_SCORE = _env_float("PREMIUM_WALLET_ELITE_SCORE", 80.0)
PREMIUM_WALLET_CORE_SCORE = _env_float("PREMIUM_WALLET_CORE_SCORE", 60.0)
PREMIUM_WALLET_WATCH_SCORE = _env_float("PREMIUM_WALLET_WATCH_SCORE", 40.0)
PREMIUM_WALLET_REMOVE_SCORE = _env_float("PREMIUM_WALLET_REMOVE_SCORE", 25.0)
PREMIUM_WALLET_MIN_TRADES_BEFORE_REMOVAL = _env_int("PREMIUM_WALLET_MIN_TRADES_BEFORE_REMOVAL", 5)
PREMIUM_WALLET_PROBATION_DAYS = _env_int("PREMIUM_WALLET_PROBATION_DAYS", 14)
PREMIUM_WALLET_MIN_PORTFOLIO_USD = _env_float("PREMIUM_WALLET_MIN_PORTFOLIO_USD", 250.0)
PREMIUM_WALLET_INACTIVITY_REMOVE_DAYS = _env_int("PREMIUM_WALLET_INACTIVITY_REMOVE_DAYS", 60)

# Sources considered "winning" tokens when mining historical holders as
# candidate smart-money wallets during discovery.
PREMIUM_DISCOVERY_MIN_WINNING_MULTIPLE = _env_float("PREMIUM_DISCOVERY_MIN_WINNING_MULTIPLE", 3.0)
PREMIUM_DISCOVERY_MIN_TRACKED_WALLET_USERS = _env_int("PREMIUM_DISCOVERY_MIN_TRACKED_WALLET_USERS", 2)

# Position sizing / liquidity preference / scam exposure scoring
# (services/premium_wallet_scorer.py) — position size below this is
# treated as too small to reflect real conviction.
PREMIUM_WALLET_MIN_POSITION_USD = _env_float("PREMIUM_WALLET_MIN_POSITION_USD", 25.0)
# Entry liquidity below this is treated as a low-liquidity/rug-prone
# pool; above the higher band is treated as the wallet consistently
# preferring safer, more liquid entries.
PREMIUM_WALLET_LOW_LIQUIDITY_USD = _env_float("PREMIUM_WALLET_LOW_LIQUIDITY_USD", 5000.0)
PREMIUM_WALLET_HIGH_LIQUIDITY_USD = _env_float("PREMIUM_WALLET_HIGH_LIQUIDITY_USD", 100000.0)
# Once a wallet's known-flagged-token exposure (honeypot/blacklisted/
# cannot-sell/etc., from services/goplus.py) reaches this percentage of
# its observed buys, it's capped below elite/core regardless of score.
PREMIUM_WALLET_SCAM_EXPOSURE_CAP_PCT = _env_float("PREMIUM_WALLET_SCAM_EXPOSURE_CAP_PCT", 20.0)


# ============================================================
# Signal Engine — verified liquidity-lock threshold
#   (domain/signals/pump_radar.py, domain/intelligence/risk_engine.py)
# ============================================================

# --- Locked Liquidity Requirement ---
# v4 fix (audit finding: this setting was documented as a "mandatory,
# non-bypassable" gate but was never actually imported or enforced
# anywhere in v3 — a second, hardcoded, duplicate threshold in
# risk_engine.py was the only one actually running). As of v4, this
# value IS the live threshold: risk_engine.LP_LOCK_REJECT_BELOW now reads
# directly from this setting instead of duplicating its own copy of the
# number. A token's real/verified LP burn-or-lock percentage (see
# domain/intelligence/lp_lock_checker.py) below this value produces a
# reject-strength finding.
#
# This does NOT hard-reject purely on "unknown"/None — that was a
# separate, earlier bug (a prior version of this exact gate silently
# rejected nearly every candidate whenever lp_lock_checker.py couldn't
# verify a value; see the history comment in pump_radar.py). When the
# real percentage can't be determined, risk_engine.py's
# assess_unverified_lock_risk() heuristic runs instead of a hard reject —
# that fallback path is unchanged by this fix.
SIGNAL_MIN_LOCKED_LIQUIDITY_PCT = _env_float("SIGNAL_MIN_LOCKED_LIQUIDITY_PCT", 50.0)

# --- Signal Deduplication (Cooldown System) ---
# Once a signal has been sent for a token, no further signal for that
# same token may be sent until this many hours have passed — regardless
# of price/volume/market-cap/smart-wallet/AI-score/trending changes in
# the meantime. Configurable within a 24-48 hour band; values outside
# that band are clamped rather than silently accepted, since going below
# 24h (or above 48h) would violate the requirement this setting exists
# to enforce.
_raw_cooldown_hours = _env_float("SIGNAL_COOLDOWN_HOURS", 24.0)
SIGNAL_COOLDOWN_HOURS = max(24.0, min(48.0, _raw_cooldown_hours))


# ============================================================
# Discovery Layer — GeckoTerminal two-lane Pump.fun discovery
#   (domain/signals/pump_radar.py: fetch_pump_fun_launches() and the
#    TWO-LANE DISCOVERY section above it;
#    domain/signals/_radar_discovery_adapter.py — RETIRED, see that
#    module's docstring)
# ============================================================
#
# Restores the original, pre-Aug-16 GeckoTerminal-primary discovery
# thesis (GeckoTerminal's new_pools + trending_pools feeds as the
# candidate source, pump.fun mint-suffix verification mandatory),
# implemented as two lanes — Fresh Momentum and Post-Launch Recovery/
# Consolidation — instead of the single flat candidate list the
# original thesis produced. The intervening DexScreener-first adapter
# (Aug 2026) has been retired; DISCOVERY_DEX_IDS, DISCOVERY_PROFILE_REQUIRED,
# and DISCOVERY_LIQUIDITY_FALLBACK_ENABLED below were specific to that
# adapter and are no longer consulted by the active discovery pipeline —
# kept defined only for backward compatibility with any existing env
# config, not because anything still reads them.
#
# These are candidate/discovery filters only — they decide whether
# AlphaPulse spends resources evaluating a token, never how it scores.
# Raw score, dynamic cutoff, and qualification are untouched by this
# section (see domain/signals/qualification.py).

DISCOVERY_CHAIN = os.getenv("DISCOVERY_CHAIN", "solana").strip().lower()

# Authoritative DEX identifiers as returned by DexScreener's own pair
# data (pair["dexId"], surfaced today as get_token_card_info()["dex"]) —
# not a heuristic like "mint address ends in pump" or a name substring
# match. Compared case-insensitively. A candidate passes this gate if its
# dex is any one of these. v4.1: both stages of a Pump.fun token's life
# are valid discovery sources now -- "pumpfun" (pre-migration bonding-
# curve pools) and "pumpswap" (post-migration pools) -- not just the
# pre-migration one. Comma-separated; e.g.
# DISCOVERY_DEX_IDS=pumpfun,pumpswap.
DISCOVERY_DEX_IDS = frozenset(
    part.strip().lower()
    for part in os.getenv("DISCOVERY_DEX_IDS", "pumpfun,pumpswap").split(",")
    if part.strip()
)

DISCOVERY_LIQUIDITY_MIN_USD = _env_float("DISCOVERY_LIQUIDITY_MIN_USD", 10_000.0)
DISCOVERY_LIQUIDITY_MAX_USD = _env_float("DISCOVERY_LIQUIDITY_MAX_USD", 150_000.0)

# Uses DexScreener's `market_cap` field specifically — never its `fdv`
# field. A candidate whose market_cap DexScreener does not report is
# rejected by the discovery layer rather than silently backfilled from
# FDV (FDV and market cap are different valuations and are not treated
# as interchangeable here).
DISCOVERY_MARKET_CAP_MIN_USD = _env_float("DISCOVERY_MARKET_CAP_MIN_USD", 30_000.0)
DISCOVERY_MARKET_CAP_MAX_USD = _env_float("DISCOVERY_MARKET_CAP_MAX_USD", 500_000.0)

# Pair age window, measured from DexScreener's own (oldest-pool)
# pairCreatedAt timestamp — NOT the same concept as DexScreener's
# "Trending: 6H" website filter, which ranks by recent activity, not
# pair age. This is the honest, supportable substitute for that
# proprietary, unreproducible ranking (see adapter module docstring).
DISCOVERY_MAX_AGE_HOURS = _env_float("DISCOVERY_MAX_AGE_HOURS", 6.0)

# Upper age bound for the Post-Launch Recovery/Consolidation lane
# (domain/signals/pump_radar.py). A candidate older than
# DISCOVERY_MAX_AGE_HOURS but no older than this is eligible for that
# lane; older than this, it is outside the discovery-relevant age range
# for either lane this cycle. Default 7 days — long enough to catch a
# genuine post-launch bounce/consolidation, bounded so discovery never
# treats an arbitrarily old token as freshly "recovering".
DISCOVERY_RECOVERY_MAX_AGE_HOURS = _env_float("DISCOVERY_RECOVERY_MAX_AGE_HOURS", 168.0)

# When True (default), a candidate whose DexScreener liquidity is
# unavailable (the normal case for pre-migration Pump.fun bonding-curve
# pairs — DexScreener has no conventional AMM pool to report reserves
# for) falls back to Solana Tracker's own liquidity figure for the same
# pool instead of being auto-rejected. Same min/max bounds apply either
# way; an unknown value from both providers still rejects the
# candidate. See domain/signals/_radar_discovery_adapter.py docstring
# "Liquidity fallback for bonding-curve pairs". Requires
# SOLANA_TRACKER_API_KEY to be configured — silently inert without it.
DISCOVERY_LIQUIDITY_FALLBACK_ENABLED = _env_bool("DISCOVERY_LIQUIDITY_FALLBACK_ENABLED", True)

# When True (default), domain.intelligence.holders.get_holder_analysis()
# sources bundle_pct (the balance-similarity "bundle" concentration figure
# consumed by domain.signals.scoring.evaluate_sybil_bundle_risk() /
# hard_reject_reasons(), including the BUNDLE_SEVERE_PCT=70% hard-reject
# gate, which this flag does not change) from Solana Tracker's authoritative
# risk.bundlers.totalPercentage (GET /tokens/{mint}) instead of this
# codebase's own +/-15% balance-similarity wallet clustering
# (BUNDLE_BALANCE_TOLERANCE / _cluster_bundles() in that module). The local
# clustering heuristic is preserved unchanged and is still used as the
# fallback whenever this is False, SOLANA_TRACKER_API_KEY isn't configured,
# or the Solana Tracker lookup fails/returns no bundlers data for a given
# token. See domain/intelligence/holders.py _resolve_bundle_risk().
BUNDLE_RISK_SOLANA_TRACKER_ENABLED = _env_bool("BUNDLE_RISK_SOLANA_TRACKER_ENABLED", True)

# Provider-resilience circuit breaker for Solana Tracker (AlphaPulse Provider
# Resilience task, 2026-08-28). Applies to every Solana Tracker call above:
# get_pool_liquidity_usd(), get_bundle_risk_pct() (providers/marketdata/
# solanatracker.py), and the paginated holder fallback (domain/intelligence/
# _solana_tracker_holder_fallback.py). See
# providers/marketdata/_provider_circuit_breaker.py for the full design.
#
# AUTH_THRESHOLD is deliberately much lower than THRESHOLD: a 401/402/403
# (bad key, plan restriction, out of credits) will not clear up on its own
# the way a timeout or a 5xx might, so there is no benefit to tolerating
# several of them before backing off — see the 2026-08-15 production
# incident referenced in domain/intelligence/_solana_tracker_holder_fallback.py.
SOLANA_TRACKER_CIRCUIT_BREAKER_THRESHOLD = _env_int("SOLANA_TRACKER_CIRCUIT_BREAKER_THRESHOLD", 3)
SOLANA_TRACKER_CIRCUIT_BREAKER_AUTH_THRESHOLD = _env_int(
    "SOLANA_TRACKER_CIRCUIT_BREAKER_AUTH_THRESHOLD", 1
)
SOLANA_TRACKER_CIRCUIT_BREAKER_COOLDOWN_SECONDS = _env_int(
    "SOLANA_TRACKER_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 120
)

# When True (default), discovery candidates come ONLY from DexScreener's
# token-profiles/latest/v1 feed — every entry there has a real,
# DexScreener-verified project profile by construction. token-boosts
# entries share the same JSON shape but boosting is a paid promotion,
# not proof of profile content, so boosts are deliberately excluded as a
# discovery source while this flag is on (see adapter module docstring
# for the reasoning; this is a documented coverage/volume trade-off, not
# an assumption that "has an image" equals "has a profile").
DISCOVERY_PROFILE_REQUIRED = _env_bool("DISCOVERY_PROFILE_REQUIRED", True)

# Upper bound on how many validated candidates one discovery cycle hands
# to PumpRadar, and (via _MAX_VALIDATIONS_PER_CYCLE in the adapter) an
# implicit cap on how many get_token_card_info() lookups one cycle can
# spend — keeps discovery a cheap pre-filter in front of expensive
# scoring rather than an unbounded crawler.
DISCOVERY_CANDIDATE_LIMIT = _env_int("DISCOVERY_CANDIDATE_LIMIT", 50)
