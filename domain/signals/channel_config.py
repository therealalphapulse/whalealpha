"""Shared Telegram destination resolution for signal-emitting engines.

Mirrors the exact parsing behavior of domain/signals/pump_radar.py's
_load_channel_ids() (comma-separated numeric chat IDs and/or @username
public channels) but is generalized to read from an arbitrary env var,
with an optional fallback env var — so new engines (Wallet Consensus,
Robinhood Discovery) can have their own configured destinations while
defaulting to the same channels the free Signal Engine already sends
to (PUMP_ALERT_CHANNEL_IDS) when nothing more specific is configured.
"""

from __future__ import annotations

import os


def parse_channel_ids(raw: str) -> list:
    """Parses a comma-separated PUMP_ALERT_CHANNEL_IDS-style string into
    a list of int chat IDs and/or '@username' strings."""
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


def load_channel_ids(env_var: str, fallback_env_var: str | None = "PUMP_ALERT_CHANNEL_IDS") -> list:
    """Reads `env_var`; if empty/unset, falls back to `fallback_env_var`
    (defaults to the free Signal Engine's own channel configuration so
    new engines work out of the box without extra setup)."""
    raw = os.getenv(env_var, "").strip()
    if not raw and fallback_env_var:
        raw = os.getenv(fallback_env_var, "").strip()
    return parse_channel_ids(raw)
