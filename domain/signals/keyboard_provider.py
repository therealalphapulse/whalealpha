"""
domain/signals/keyboard_provider.py

NEW in v4 — the concrete fix for the layering violation the audit
confirmed twice (§1/§2 of the audit; §4 of the Bible): both
`pump_radar.py` and `alert_engine.py` imported
`app_platform.keyboards.token_actions` directly, meaning the domain/
service layer reached up into the presentation layer.

The fix is dependency injection, not a rewrite of the keyboard itself:
domain/signals code needs *some* inline keyboard attached to an alert
message, but it should not need to know which concrete keyboard-building
function the presentation layer uses to build it. `app_platform` wires
the real `token_actions_keyboard` implementation into this module once at
startup (see app_platform/gateway/app.py); domain/signals code only ever
calls through `build_token_actions_keyboard()` below.

If nothing has wired a factory yet (e.g. a domain-layer unit test that
imports pump_radar.py without booting the full app), this degrades to
"no keyboard" rather than raising — alerts still send, just without
inline buttons, which is a safe degradation for a background job.
"""

from __future__ import annotations

from typing import Any, Callable

_keyboard_factory: Callable[..., Any] | None = None


def set_keyboard_factory(factory: Callable[..., Any]) -> None:
    """Called once by app_platform.gateway.app at startup, wiring the real
    `token_actions_keyboard` implementation in. Import-boundary-safe:
    app_platform imports domain/, never the reverse."""
    global _keyboard_factory
    _keyboard_factory = factory


def build_token_actions_keyboard(
    contract: str,
    pair_url: str | None,
    *,
    website_url: str | None = None,
    twitter_url: str | None = None,
    telegram_url: str | None = None,
):
    if _keyboard_factory is None:
        return None
    return _keyboard_factory(
        contract,
        pair_url,
        website_url=website_url,
        twitter_url=twitter_url,
        telegram_url=telegram_url,
    )
