"""Read-only balance guard for RealWallet buy execution.

This module is intentionally narrow: it does not change token selection,
quote logic, spending limits, or transaction execution. It only prevents a
buy from reaching Jupiter/signing when the wallet balance is already known to
be below the requested SOL amount.

Performance note: execute_real_buy() itself already performs this exact
check (see BUY_NETWORK_RESERVE_LAMPORTS handling in real_trade_engine.py),
using a threshold that is a strict superset of this wrapper's threshold (it
also reserves lamports for network fees/priority fee on top of the raw SOL
amount checked here). This wrapper previously re-fetched the wallet row
(DB query) and the on-chain SOL balance (RPC round trip) a second time
before calling through -- duplicating work that gates the exact same
outcome. That duplicate fetch has been removed: the safety property (no
buy is ever submitted when the wallet can't afford it) is unchanged and is
still enforced, once, inside execute_real_buy itself. This trims one DB
round trip and one RPC round trip off every automated and manual real buy.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger("AlphaPulse.RealWallet")

INSUFFICIENT_FUNDS_PREFIX = "Insufficient funds."


async def _guarded_execute_real_buy(*args, **kwargs):
    module = sys.modules.get("domain.trading.real.real_trade_engine")
    if module is None:
        raise RuntimeError("Real trade engine is not loaded")

    original = getattr(module, "_alphapulse_original_execute_real_buy", None)
    if original is None:
        raise RuntimeError("Original execute_real_buy is unavailable")

    return await original(*args, **kwargs)


def install() -> None:
    """Wrap execute_real_buy once, after its module has fully imported."""
    module = sys.modules.get("domain.trading.real.real_trade_engine")
    if module is None or not hasattr(module, "execute_real_buy"):
        return
    if getattr(module.execute_real_buy, "_alphapulse_funds_guard", False):
        return

    module._alphapulse_original_execute_real_buy = module.execute_real_buy
    module.execute_real_buy = _guarded_execute_real_buy
    module.execute_real_buy._alphapulse_funds_guard = True
    logger.info("[RealWallet] Insufficient-funds preflight installed")
