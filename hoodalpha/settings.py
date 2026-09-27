"""
hoodalpha/settings.py

HoodAlpha's own configuration. Deliberately does NOT import
config/settings.py -- every var here is read directly from the
environment so this package has zero coupling to WhaleAlpha's config
module (which can be refactored freely without ever touching this
file).

DATABASE_URL and REDIS_URL are the two exceptions: HoodAlpha is meant
to point at the SAME Postgres/Redis instances WhaleAlpha uses (per the
approved plan), so it reads the same env var NAMES WhaleAlpha's three
services already use. In Railway, HoodAlpha's own services get their
own copies of these variables (e.g. via `${{Postgres.DATABASE_URL}}` /
`${{Redis.REDIS_URL}}` reference variables), exactly like WhaleAlpha's
three services already do -- this is config duplication, not shared
runtime state.
"""

from __future__ import annotations

import os


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _parse_chat_ids(raw: str) -> list:
    """Same comma-separated-int-or-@username parsing WhaleAlpha's
    domain/signals/channel_config.py uses -- copied, not imported, so
    a change to that file can never affect HoodAlpha's delivery list."""
    raw = (raw or "").strip()
    if not raw:
        return []
    ids: list = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if item.startswith("@"):
            ids.append(item)
            continue
        try:
            ids.append(int(item))
        except ValueError:
            pass
    return ids


# --- Telegram (own bot, own token, never WhaleAlpha's BOT_TOKEN) ---
HOOD_BOT_TOKEN = os.getenv("HOOD_BOT_TOKEN", "").strip()
HOOD_TELEGRAM_CHAT_IDS = _parse_chat_ids(os.getenv("HOOD_TELEGRAM_CHAT_IDS", ""))

# --- Shared infra (same Postgres/Redis instance as WhaleAlpha; own schema/keys) ---
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
REDIS_URL = os.getenv("REDIS_URL", "").strip()

# --- Strategy thresholds ---
# A dip must be at least this many percent below the reference
# (WhaleAlpha's entry_price at signal creation) before HoodAlpha starts
# treating it as a "meaningful dip" worth arming for a recovery alert.
# Prevents 1-2% noise from ever arming the rise check.
HOOD_MIN_DIP_PCT = _env_float("HOOD_MIN_DIP_PCT", 10.0)

# Fire a HoodAlpha signal once price has risen this many percent off the
# lowest confirmed dip price.
HOOD_RISE_THRESHOLD_PCT = _env_float("HOOD_RISE_THRESHOLD_PCT", 20.0)

# --- Loop cadence ---
HOOD_INGEST_INTERVAL_SECONDS = _env_int("HOOD_INGEST_INTERVAL_SECONDS", 120)
HOOD_MONITOR_INTERVAL_SECONDS = _env_int("HOOD_MONITOR_INTERVAL_SECONDS", 90)

# How many rows ingest.py pulls from signal_tokens per cycle.
HOOD_INGEST_BATCH_SIZE = _env_int("HOOD_INGEST_BATCH_SIZE", 500)

# A watch with no price update for this many hours is marked "stale"
# and excluded from further polling (bounds the always-polled set).
HOOD_STALE_AFTER_HOURS = _env_int("HOOD_STALE_AFTER_HOURS", 168)  # 7 days

# When a watched token has fallen out of WhaleAlpha's active signal set
# (piggyback read returns nothing), optionally fall back to HoodAlpha's
# own direct DexScreener call instead of immediately going stale.
HOOD_FALLBACK_DEXSCREENER_ENABLED = _env_bool("HOOD_FALLBACK_DEXSCREENER_ENABLED", True)

# Prefix isolates HoodAlpha's Redis lock keys from WhaleAlpha's
# ("loop:alert_engine", etc.) even though they share one Redis instance.
HOOD_LOCK_PREFIX = "hoodalpha:"
