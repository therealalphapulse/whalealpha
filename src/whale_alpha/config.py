"""Environment configuration — port of src/config/env.ts (Zod) to pydantic-settings.

Behavior preserved:
  * Missing/invalid env vars raise a clear, loud startup error (Zod -> pydantic
    ValidationError), not a silent default or a crash deep in unrelated code.
  * Same field names, same defaults, same validation rules (regex for
    ENCRYPTION_KEY, url validation, enums, numeric coercion).
  * `admin_telegram_ids` is derived exactly like the TS `adminTelegramIds` Set:
    split TELEGRAM_ADMIN_CHAT_IDS on commas, strip whitespace, drop empties.

Difference from the TS version: pydantic-settings reads from process env (and,
if present, a `.env` file) the same way `dotenv/config` + `zod` did; we keep the
same env var names so `.env` files are drop-in compatible.
"""

from __future__ import annotations

import os
import re
import sys
from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENCRYPTION_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    NODE_ENV: Literal["development", "test", "production"] = "development"
    LOG_LEVEL: str = "info"

    TELEGRAM_BOT_TOKEN: str = Field(..., min_length=1)
    TELEGRAM_ADMIN_CHAT_IDS: str = ""

    DATABASE_URL: str = Field(..., min_length=1)
    REDIS_URL: str = "redis://localhost:6379"

    SOLANA_RPC_URL: str
    SOLANA_WS_URL: str | None = None
    SOLANA_CLUSTER: Literal["mainnet-beta", "devnet", "testnet"] = "mainnet-beta"

    # --- Solana RPC redundancy / failover (see integrations/solana_connection.py) ---
    # Every Solana RPC call made through create_connection() now transparently
    # fails over across multiple providers instead of hard-depending on
    # SOLANA_RPC_URL alone. SOLANA_RPC_URL is always tried first; these add
    # more endpoints, tried in order only after an earlier one errors.
    #
    # Comma-separated list of additional full RPC URLs (any provider, or a
    # self-hosted node) — e.g. "https://rpc.example.com,https://rpc2.example.com".
    SOLANA_RPC_FALLBACK_URLS: str = ""
    # Convenience: set just the API key and the provider's standard Solana
    # mainnet URL is built automatically, instead of hand-writing it into
    # SOLANA_RPC_FALLBACK_URLS. Leave unset to skip that provider.
    DRPC_API_KEY: str | None = None
    ALCHEMY_API_KEY: str | None = None
    ANKR_API_KEY: str | None = None
    # Max endpoints tried (in order) for a single RPC call before giving up.
    # 1 disables failover entirely (matches pre-failover behavior).
    SOLANA_RPC_MAX_FAILOVER_ATTEMPTS: int = Field(4, ge=1)
    # "balanced" (default): route each RPC call to whichever configured
    # provider best suits its workload — e.g. account/token-holder lookups
    # prefer secondary providers (to keep that high-volume traffic off the
    # primary node), while transaction submission and signature/tx-history
    # reads prefer the primary node (latency + correctness sensitive). See
    # integrations/solana_connection.py's _ROLE_PRIORITY_BY_WORKLOAD.
    # "primary_first": opt out of workload-aware routing — every call tries
    # SOLANA_RPC_URL first, then falls through the rest in the order they
    # were configured, exactly like the original flat failover behavior.
    SOLANA_RPC_ROUTING_STRATEGY: Literal["balanced", "primary_first"] = "balanced"

    JUPITER_API_BASE: str = "https://quote-api.jup.ag/v6"
    # Optional override for a paid/self-hosted price feed. When unset, the
    # price feed integration (integrations/price_feed.py) falls back to
    # Jupiter's public Price API — see that module's docstring for the exact
    # request/response shape assumed and how to point this at a different
    # provider.
    PRICE_FEED_API_BASE: str | None = None
    PRICE_FEED_API_KEY: str | None = None
    PRICE_CACHE_TTL_SECONDS: float = Field(15, ge=1)

    # Jupiter Tokens API V2 — used only by the discovery engine's
    # find_candidates_from_trending_tokens bootstrap source (see
    # integrations/wallet_discovery_source.py). Falls back to
    # PRICE_FEED_API_KEY if unset, since a single portal.jup.ag key covers
    # both Price V3 and Tokens V2 on the same account — set JUPITER_API_KEY
    # explicitly only if you're using a different key for this than pricing.
    JUPITER_TOKENS_API_BASE: str = "https://api.jup.ag/tokens/v2"
    JUPITER_API_KEY: str | None = None

    ENCRYPTION_KEY: str
    JWT_SECRET: str = Field(..., min_length=16)

    SIGNAL_MIN_WALLETS: int = Field(3, ge=1)
    SIGNAL_WINDOW_MINUTES: int = Field(30, ge=1)
    SIGNAL_MIN_CONFIDENCE: float = Field(65, ge=0, le=100)

    # --- AI-written signal explanations (engines/ai_insight.py) ---
    # Purely cosmetic on top of the deterministic score/risk_level above —
    # never used to gate whether a signal fires. Leave ANTHROPIC_API_KEY unset
    # to keep the old templated ai_recommendation strings from signal.py.
    ANTHROPIC_API_KEY: str | None = None
    AI_INSIGHTS_ENABLED: bool = True
    AI_INSIGHTS_MODEL: str = "claude-haiku-4-5"
    AI_INSIGHTS_TIMEOUT_SECONDS: float = Field(8.0, ge=1)

    DEFAULT_MAX_SLIPPAGE_BPS: int = 150
    DEFAULT_MAX_DAILY_TRADES: int = 10
    DEFAULT_MAX_DAILY_EXPOSURE_USD: float = 500

    # --- Whale ingestion webhook (feature: whale wallet tracking) ---
    # Inbound HTTP receiver for an indexer (Helius enhanced webhooks by
    # default — see integrations/helius_webhook.py) that POSTs tracked-wallet
    # activity. The bot itself stays long-polling; this is a *separate* small
    # aiohttp server run alongside it (see main.py).
    WEBHOOK_HOST: str = "0.0.0.0"
    WEBHOOK_PORT: int = Field(8080, ge=1, le=65535)
    WEBHOOK_PATH: str = "/webhooks/helius"
    # Shared secret Helius echoes back in the `Authorization` header when you
    # configure "Authentication Header" on the webhook in the Helius
    # dashboard. Required in production so the endpoint can't be spoofed.
    HELIUS_WEBHOOK_SECRET: str | None = None

    # --- % price-increase alerts (feature: price alerts) ---
    PRICE_ALERT_INTERVAL_SECONDS: float = Field(60, ge=5)
    PRICE_ALERT_MIN_COOLDOWN_MINUTES: float = Field(15, ge=0)

    # --- Whale Wallet Discovery & Intelligence Engine (feature: discovery) ---
    # See engines/discovery.py. This is the ONLY thing allowed to add wallets
    # to whale_wallets without a human admin behind it — every threshold below
    # is deliberately configuration, not a code constant, so ops can tune the
    # engine without a deploy.
    DISCOVERY_ENABLED: bool = True
    DISCOVERY_INTERVAL_SECONDS: float = Field(900, ge=30)  # 15 min between cycles by default
    # Delay before the FIRST discovery cycle after startup. Kept short (and
    # deliberately separate from DISCOVERY_INTERVAL_SECONDS) so the engine
    # produces its first observable log line — and starts sourcing/scoring
    # candidates — within seconds of boot instead of only after a full
    # 15-minute interval has elapsed. See start_discovery_loop.
    DISCOVERY_STARTUP_DELAY_SECONDS: float = Field(15, ge=0)

    # Target population bounds for the tracked (APPROVED) wallet database.
    DISCOVERY_MIN_TRACKED_WALLETS: int = Field(500, ge=1)
    DISCOVERY_MAX_TRACKED_WALLETS: int = Field(1500, ge=1)

    # Minimum bar a brand-new candidate must clear to be auto-promoted
    # straight to APPROVED. Intentionally stricter than
    # engines.scoring.MIN_APPROVED_SCORE (the bar for an *existing* tracked
    # wallet to stay APPROVED) — a wallet with no track record in our system
    # yet should have to prove more before it can move real signal weight.
    DISCOVERY_MIN_SCORE_TO_APPROVE: float = Field(55, ge=0, le=100)
    DISCOVERY_MIN_ROI_30D: float = Field(0.15, ge=-1)
    DISCOVERY_MIN_WIN_RATE: float = Field(0.5, ge=0, le=1)
    DISCOVERY_MIN_TRADE_COUNT_30D: int = Field(10, ge=1)
    DISCOVERY_MIN_WALLET_AGE_DAYS: int = Field(14, ge=0)

    # A tracked wallet with no on-chain activity for this long is treated as
    # dormant and retired regardless of its last score, freeing a slot.
    DISCOVERY_INACTIVITY_TIMEOUT_DAYS: int = Field(21, ge=1)
    # A wallet must score below the approval bar for this many *consecutive*
    # re-scoring cycles before it's retired — avoids flapping a good wallet
    # out over one noisy cycle (e.g. a temporary metrics-fetch hiccup).
    DISCOVERY_LOW_SCORE_CYCLES_BEFORE_RETIRE: int = Field(3, ge=1)

    # Rate-limit / batching knobs so a run with hundreds of tracked + queued
    # candidate wallets doesn't hammer the RPC/indexer in one cycle.
    DISCOVERY_CANDIDATE_BATCH_SIZE: int = Field(50, ge=1)
    DISCOVERY_RESCORE_BATCH_SIZE: int = Field(100, ge=1)
    DISCOVERY_CANDIDATE_MIN_REEVAL_HOURS: float = Field(12, ge=0)
    DISCOVERY_SOURCE_TOKEN_LOOKBACK: int = Field(20, ge=1)
    DISCOVERY_MAX_HOLDERS_PER_TOKEN: int = Field(50, ge=1)
    # Client-side pacing + retry for the Solana RPC calls behind holder
    # resolution (integrations.solana_connection.get_token_largest_accounts).
    # A discovery cycle can resolve holders for ~20 tokens in a row, which
    # without pacing fires hundreds of RPC calls in a tight burst — over
    # most providers' rate limits (Helius's RPC endpoint included). See that
    # function's docstring for the production incident this fixes.
    DISCOVERY_RPC_MIN_INTERVAL_SECONDS: float = Field(0.12, ge=0)
    DISCOVERY_RPC_MAX_RETRIES: int = Field(3, ge=0)

    # Rate-limit resilience for HTTP wallet-history / trending-token
    # providers (Helius, Jupiter) — see utils/http_retry.py and
    # integrations/wallet_discovery_source.fetch_wallet_swap_history. A 429
    # here previously meant an immediate, permanent candidate rejection
    # (NO_HISTORY_PROVIDER_OR_FETCH_FAILED) even for a wallet that might be
    # perfectly good — this is the production fix.
    DISCOVERY_HISTORY_MAX_CONCURRENCY: int = Field(5, ge=1)
    DISCOVERY_HISTORY_MAX_RETRIES: int = Field(3, ge=0)
    DISCOVERY_HISTORY_RETRY_BASE_SECONDS: float = Field(1.0, ge=0)
    DISCOVERY_HISTORY_RETRY_MAX_SECONDS: float = Field(20.0, ge=0)
    DISCOVERY_HISTORY_CACHE_TTL_SECONDS: float = Field(300, ge=0)
    DISCOVERY_HISTORY_NEGATIVE_CACHE_TTL_SECONDS: float = Field(3600, ge=0)
    # After a candidate's history fetch fails transiently this many times
    # (across cycles — a fresh retry is only attempted once its computed
    # backoff window, tracked per-candidate, has elapsed), it's permanently
    # marked EVALUATED/NO_HISTORY instead of staying in the retry queue
    # forever. See WalletCandidate.history_retry_count.
    DISCOVERY_HISTORY_MAX_RETRIES_BEFORE_REJECT: int = Field(5, ge=0)

    # Wallet-history fallback chain (Helius unreachable/rate-limited):
    # Primary (Helius) -> stale cache -> RPC-reconstructed history -> retry
    # queue. See integrations/wallet_discovery_source.fetch_wallet_swap_history.
    DISCOVERY_HISTORY_STALE_CACHE_TTL_SECONDS: float = Field(21600, ge=0)  # 6h
    DISCOVERY_HISTORY_RPC_FALLBACK_ENABLED: bool = Field(True)
    DISCOVERY_HISTORY_RPC_FALLBACK_MAX_SIGNATURES: int = Field(40, ge=1, le=200)
    # Downstream scoring discount applied when history came from a fallback
    # (stale cache or RPC reconstruction) rather than the primary provider —
    # see evaluate_candidates' confidence adjustment.
    DISCOVERY_HISTORY_FALLBACK_CONFIDENCE_MULTIPLIER: float = Field(0.7, ge=0, le=1)

    # Shared HTTP retry/circuit-breaker layer for every discovery-source
    # provider other than Helius wallet-history (Jupiter, Birdeye,
    # DexScreener, pump.fun, LaunchLab, Raydium, Meteora) — see
    # utils/http_retry.ProviderClient / get_provider_client.
    DISCOVERY_PROVIDER_MAX_CONCURRENCY: int = Field(4, ge=1)
    DISCOVERY_PROVIDER_MAX_RETRIES: int = Field(3, ge=0)
    DISCOVERY_PROVIDER_RETRY_BASE_SECONDS: float = Field(1.0, ge=0)
    DISCOVERY_PROVIDER_RETRY_MAX_SECONDS: float = Field(20.0, ge=0)
    DISCOVERY_PROVIDER_CACHE_TTL_SECONDS: float = Field(120, ge=0)
    DISCOVERY_PROVIDER_CIRCUIT_FAILURE_THRESHOLD: int = Field(5, ge=1)
    DISCOVERY_PROVIDER_CIRCUIT_COOLDOWN_SECONDS: float = Field(60.0, ge=0)

    # --- Trending-token bootstrap source (feature: discovery cold start) ---
    # Independent of anything already tracked — see
    # integrations/wallet_discovery_source.find_candidates_from_trending_tokens.
    # Without this, discovery can ONLY source candidates from holders of
    # tokens your own tracked whales already bought (see discover_candidates
    # in engines/discovery.py), which is a closed loop: zero tracked wallets
    # -> zero Signals -> zero candidates, forever. This source breaks that
    # loop by pulling from Jupiter's platform-wide trending/most-traded
    # tokens instead, so the engine can find its first wallets with no admin
    # seeding required.
    DISCOVERY_TRENDING_ENABLED: bool = True
    DISCOVERY_TRENDING_CATEGORY: Literal["toporganicscore", "toptraded", "toptrending"] = "toptraded"
    DISCOVERY_TRENDING_INTERVAL: Literal["5m", "1h", "6h", "24h"] = "1h"
    DISCOVERY_TRENDING_TOKEN_LIMIT: int = Field(20, ge=1, le=100)

    # Optional: Helius API key, used by integrations/wallet_discovery_source.py
    # for wallet transaction-history lookups. Falls back to plain Solana RPC
    # (top-holder based discovery only, no historical PnL) when unset — see
    # that module's docstring.
    HELIUS_API_KEY: str | None = None
    HELIUS_API_BASE: str = "https://api.helius.xyz"

    # --- Hybrid Discovery Engine: on-chain launch sources (Priority 1) ---
    # See integrations/free_market_sources.py. Each source is independently
    # toggleable and failure-isolated — a dead/rate-limited provider never
    # stops the other sources or the discovery cycle. These are the highest
    # priority sourcing streams: fresh liquidity events and their early
    # buyers/accumulators, which need no tracked wallets or Signals to exist
    # (unlike the legacy signaled-token-holders stream), so they are what
    # actually eliminates the cold-start loop end to end.
    DISCOVERY_PUMPFUN_ENABLED: bool = True
    DISCOVERY_PUMPFUN_API_BASE: str = "https://frontend-api.pump.fun"
    DISCOVERY_LAUNCHLAB_ENABLED: bool = True
    DISCOVERY_LAUNCHLAB_API_BASE: str = "https://api-v3.raydium.io/launchlab"
    DISCOVERY_RAYDIUM_ENABLED: bool = True
    DISCOVERY_RAYDIUM_API_BASE: str = "https://api-v3.raydium.io"
    DISCOVERY_METEORA_ENABLED: bool = True
    DISCOVERY_METEORA_API_BASE: str = "https://amm-v2.meteora.ag"
    # How far back (minutes) an on-chain launch source looks for "fresh"
    # pools/mints each cycle. Kept short — this stream is meant to catch
    # launches near-real-time, not backfill history.
    DISCOVERY_ONCHAIN_LAUNCH_LOOKBACK_MINUTES: int = Field(30, ge=1)
    DISCOVERY_MAX_LAUNCHES_PER_SOURCE: int = Field(15, ge=1)

    # --- Trending-token fallback chain (Priority 2) ---
    # DISCOVERY_TRENDING_* above already configures the Jupiter leg (tried
    # first). If Jupiter has no key configured or its request fails, the
    # engine falls through this ordered list of free-tier providers before
    # giving up on trending-token sourcing for the cycle — see
    # integrations/free_market_sources.find_trending_tokens_multi_provider.
    # Never a hard dependency on any single one of them.
    DISCOVERY_BIRDEYE_ENABLED: bool = True
    DISCOVERY_BIRDEYE_API_BASE: str = "https://public-api.birdeye.so"
    BIRDEYE_API_KEY: str | None = None
    DISCOVERY_DEXSCREENER_ENABLED: bool = True
    DISCOVERY_DEXSCREENER_API_BASE: str = "https://api.dexscreener.com"

    # --- Wallet Graph Expansion (Priority 4) ---
    # Every promoted wallet becomes a discovery node: its recent traded
    # tokens are re-queried for co-holders/co-buyers, and repeated
    # co-occurrence across distinct tokens raises a relationship-strength
    # score (engines/wallet_graph.py) that feeds new-candidate confidence.
    # Uses the existing Postgres schema (a new lightweight table, no graph
    # database) per the architecture requirements.
    DISCOVERY_GRAPH_EXPANSION_ENABLED: bool = True
    DISCOVERY_GRAPH_EXPANSION_BATCH_SIZE: int = Field(20, ge=1)
    DISCOVERY_GRAPH_MAX_TOKENS_PER_WALLET: int = Field(5, ge=1)
    # A related wallet must co-occur across at least this many distinct
    # shared tokens before it's queued as its own candidate — a single
    # shared token is weak evidence (could be coincidence on a hot token),
    # repeated co-trading across several is not.
    DISCOVERY_GRAPH_MIN_COOCCURRENCE: int = Field(2, ge=1)

    # --- Blockchain-first Discovery Engine (Phase 1 refactor) ---
    # See integrations/chain_scanner.py. This is now the PRIMARY wallet
    # discovery source: it scans recent Solana blocks directly via RPC and
    # extracts trader wallet addresses from swap/migration transactions
    # (Jupiter, Raydium AMM/CLMM/CPMM, Pump.fun bonding-curve + PumpSwap
    # migrations, LaunchLab). Every other candidate-sourcing HTTP API below
    # (DISCOVERY_PUMPFUN_ENABLED, DISCOVERY_TRENDING_ENABLED, Birdeye,
    # DexScreener, ...) is gated off discovery by DISCOVERY_API_SOURCES_ENABLED
    # (default False) — those provider integrations are kept in place
    # unmodified for later enrichment use, they're just no longer part of
    # the wallet *discovery* pipeline per the Phase 1 requirement that only
    # the chain itself may surface a new candidate wallet.
    DISCOVERY_BLOCKCHAIN_SCAN_ENABLED: bool = True
    # Master gate for every non-blockchain (HTTP market-data API) candidate
    # source that predates this refactor (_ON_CHAIN_LAUNCH_SOURCES,
    # the Jupiter/Birdeye/DexScreener trending fallback chain). False by
    # default: those functions remain fully intact and reusable, but
    # discover_candidates no longer calls them to source new wallets.
    DISCOVERY_API_SOURCES_ENABLED: bool = False
    # How many new slots to fetch per discovery cycle. Deliberately small —
    # never "scan the whole chain" — so one cycle's RPC usage is bounded
    # regardless of how far behind the scanner has fallen.
    DISCOVERY_BLOCK_SCAN_BATCH_SIZE: int = Field(20, ge=1, le=200)
    # Hard ceiling on how many slots one cycle will attempt to catch up by,
    # even if the persisted checkpoint is very stale (e.g. the process was
    # down for hours) — protects free RPC tiers from a huge backlog replay;
    # the scanner just catches up gradually over several cycles instead.
    DISCOVERY_BLOCK_SCAN_MAX_CATCHUP_SLOTS: int = Field(400, ge=1)
    # Where to start on the very first run (no persisted checkpoint yet):
    # current tip minus this many slots, rather than genesis.
    DISCOVERY_BLOCK_SCAN_INITIAL_LOOKBACK_SLOTS: int = Field(50, ge=1)
    # Concurrent in-flight getBlock RPC calls (asyncio.Semaphore) within one
    # batch — bounds burst RPC usage independently of batch size.
    DISCOVERY_BLOCK_SCAN_CONCURRENCY: int = Field(4, ge=1)
    DISCOVERY_BLOCK_SCAN_MAX_RETRIES: int = Field(3, ge=0)
    DISCOVERY_BLOCK_SCAN_RETRY_BASE_SECONDS: float = Field(1.0, ge=0)
    DISCOVERY_BLOCK_SCAN_RETRY_MAX_SECONDS: float = Field(20.0, ge=0)
    # Short-TTL cache for the chain-tip (`getSlot`) lookup — avoids an extra
    # RPC round trip if it's queried more than once within the same cycle.
    DISCOVERY_BLOCK_SCAN_TIP_CACHE_TTL_SECONDS: float = Field(10, ge=0)
    # Per-block cap on how many distinct trader wallets are extracted — a
    # single busy block can contain thousands of swaps; this keeps one
    # block from dominating a whole cycle's candidate budget.
    DISCOVERY_BLOCK_SCAN_MAX_WALLETS_PER_BLOCK: int = Field(200, ge=1)
    # Extra, comma-separated Solana program IDs to treat as swap programs,
    # in addition to the built-in Jupiter/Raydium/Pump.fun/LaunchLab set
    # (see integrations/chain_scanner.SWAP_PROGRAM_IDS) — lets ops track a
    # newly-launched DEX without a code change/deploy.
    DISCOVERY_BLOCK_SCAN_EXTRA_PROGRAM_IDS: str = ""

    @field_validator(
        "SOLANA_RPC_URL",
        "JUPITER_API_BASE",
        "DISCOVERY_PUMPFUN_API_BASE",
        "DISCOVERY_LAUNCHLAB_API_BASE",
        "DISCOVERY_RAYDIUM_API_BASE",
        "DISCOVERY_METEORA_API_BASE",
        "DISCOVERY_BIRDEYE_API_BASE",
        "DISCOVERY_DEXSCREENER_API_BASE",
    )
    @classmethod
    def _must_be_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("must be a valid URL")
        return v

    @field_validator("PRICE_FEED_API_BASE", "HELIUS_API_BASE", "JUPITER_TOKENS_API_BASE")
    @classmethod
    def _optional_url(cls, v: str | None) -> str | None:
        if v is not None and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("must be a valid URL")
        return v

    @field_validator("SOLANA_RPC_FALLBACK_URLS")
    @classmethod
    def _fallback_urls_must_be_valid(cls, v: str) -> str:
        for raw in v.split(","):
            url = raw.strip()
            if url and not (url.startswith("http://") or url.startswith("https://")):
                raise ValueError(f"SOLANA_RPC_FALLBACK_URLS entry {url!r} must be a valid URL")
        return v

    @field_validator("DISCOVERY_MAX_TRACKED_WALLETS")
    @classmethod
    def _max_tracked_gte_min(cls, v: int, info) -> int:  # noqa: ANN001 — pydantic v2 ValidationInfo
        min_tracked = info.data.get("DISCOVERY_MIN_TRACKED_WALLETS")
        if min_tracked is not None and v < min_tracked:
            raise ValueError("DISCOVERY_MAX_TRACKED_WALLETS must be >= DISCOVERY_MIN_TRACKED_WALLETS")
        return v

    @field_validator("ENCRYPTION_KEY")
    @classmethod
    def _must_be_64_hex(cls, v: str) -> str:
        if not _ENCRYPTION_KEY_RE.match(v):
            raise ValueError("ENCRYPTION_KEY must be 64 hex chars (32 bytes)")
        return v

    @property
    def admin_telegram_ids(self) -> set[str]:
        return {s.strip() for s in self.TELEGRAM_ADMIN_CHAT_IDS.split(",") if s.strip()}

    @property
    def effective_webhook_port(self) -> int:
        """Port the Helius webhook server actually binds to.

        Railway injects a `PORT` env var (and, if a public domain / TCP
        proxy is attached to the service, routes its healthcheck and
        traffic to it) that can differ from whatever fixed `WEBHOOK_PORT`
        was configured. Preferring `PORT` when present means the webhook
        server — and Railway's optional `/healthz` healthcheck — line up
        with Railway's networking automatically, without requiring the
        "Target Port" to be manually kept in sync in the dashboard.
        """
        port = os.environ.get("PORT")
        if port:
            try:
                return int(port)
            except ValueError:
                return self.WEBHOOK_PORT
        return self.WEBHOOK_PORT


def load_env() -> Env:
    try:
        return Env()  # type: ignore[call-arg]
    except ValidationError as exc:
        # Mirror the TS loadEnv(): log a clear, structured error and raise so the
        # process refuses to start with a bad/missing configuration.
        print("Invalid environment configuration:", file=sys.stderr)
        print(exc, file=sys.stderr)
        raise RuntimeError(
            "Environment validation failed. Check .env against .env.example."
        ) from exc


@lru_cache(maxsize=1)
def get_env() -> Env:
    """Cached singleton, analogous to the TS module-level `export const env`."""
    return load_env()
