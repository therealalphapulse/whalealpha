"""
infra/observability/error_tracking.py

NEW in v4 (Bible §10). The audit found zero external error tracking
anywhere in the codebase — only the ~245 broad `except Exception` blocks
(many of them effectively silent) that the audit's code-quality section
flagged. This does not rewrite any of those blocks; it wires up Sentry
(or an equivalent, if swapped later) so unhandled exceptions and, if
call sites are updated to use `capture_exception` inside existing
`except` blocks, currently-swallowed errors too, become visible outside
of manually reading logs.

No-ops (logs a warning once) if `sentry_sdk` isn't installed or
`SENTRY_DSN` isn't set — matches the pattern used throughout infra/ for
every other optional-in-dev, required-in-production integration.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("AlphaPulse.ErrorTracking")

_initialized = False


def configure_error_tracking() -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("SENTRY_DSN not set — error tracking disabled.")
        return

    try:
        import sentry_sdk
    except ImportError:
        logger.warning(
            "SENTRY_DSN is set but 'sentry_sdk' is not installed; error "
            "tracking disabled. Install it (pip install sentry-sdk)."
        )
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("ENVIRONMENT", "production"),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
    )
    logger.info("Sentry error tracking initialized (environment=%s)", os.getenv("ENVIRONMENT", "production"))


def capture_exception(exc: BaseException) -> None:
    """Call from an existing `except Exception as exc:` block to report it
    to Sentry without changing that block's existing fallback/None-return
    behavior — additive, not a replacement for the current error-handling
    convention documented in multi_rpc_manager.py."""
    if not _initialized:
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except ImportError:
        pass
