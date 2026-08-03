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

    @field_validator("SOLANA_RPC_URL", "JUPITER_API_BASE")
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
