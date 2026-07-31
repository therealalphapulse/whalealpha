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

    JUPITER_API_BASE: str = "https://quote-api.jup.ag/v6"
    PRICE_FEED_API_BASE: str | None = None
    PRICE_FEED_API_KEY: str | None = None

    ENCRYPTION_KEY: str
    JWT_SECRET: str = Field(..., min_length=16)

    SIGNAL_MIN_WALLETS: int = Field(3, ge=1)
    SIGNAL_WINDOW_MINUTES: int = Field(30, ge=1)
    SIGNAL_MIN_CONFIDENCE: float = Field(65, ge=0, le=100)

    DEFAULT_MAX_SLIPPAGE_BPS: int = 150
    DEFAULT_MAX_DAILY_TRADES: int = 10
    DEFAULT_MAX_DAILY_EXPOSURE_USD: float = 500

    @field_validator("SOLANA_RPC_URL", "JUPITER_API_BASE")
    @classmethod
    def _must_be_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("must be a valid URL")
        return v

    @field_validator("PRICE_FEED_API_BASE")
    @classmethod
    def _optional_url(cls, v: str | None) -> str | None:
        if v is not None and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("must be a valid URL")
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
