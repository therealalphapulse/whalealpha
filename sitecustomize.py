"""Production compatibility hooks for holder V2, RealWallet safety, and value display.

Holder V2 compatibility is installed only after domain.intelligence.holders has
finished importing. The RealWallet funds guard is installed only after the
real trade engine has finished importing. The RealWallet value display is
installed only after the Telegram Real Wallet command module has imported.
These are narrow runtime adapters; they do not change PumpRadar token
filters or scoring thresholds.
"""

from __future__ import annotations

import builtins
import logging
import sys

logger = logging.getLogger("AlphaPulse.Holders")
_original_import = builtins.__import__
_installing_holder = False
_installing_real_wallet = False
_installing_wallet_value_display = False


def _install_if_ready() -> None:
    global _installing_holder
    if _installing_holder or "domain.intelligence.holders" not in sys.modules:
        return

    holders = sys.modules.get("domain.intelligence.holders")
    if holders is None or not hasattr(holders, "_fetch_via_program_accounts_v2"):
        return

    _installing_holder = True
    try:
        from domain.intelligence._holder_v2_compat import install as install_v2
        install_v2()
    except Exception as exc:
        logger.warning("[HolderDiag] Helius V2 compatibility adapter install deferred: %s", exc)
    finally:
        _installing_holder = False


def _install_real_wallet_guard_if_ready() -> None:
    global _installing_real_wallet
    if _installing_real_wallet or "domain.trading.real.real_trade_engine" not in sys.modules:
        return

    _installing_real_wallet = True
    try:
        from domain.trading.real._funds_guard import install
        install()
    except Exception as exc:
        logger.warning("[RealWallet] Insufficient-funds guard install deferred: %s", exc)
    finally:
        _installing_real_wallet = False


def _install_wallet_value_display_if_ready() -> None:
    global _installing_wallet_value_display
    if _installing_wallet_value_display or "app_platform.commands.real_wallet" not in sys.modules:
        return

    _installing_wallet_value_display = True
    try:
        from domain.intelligence.real_wallet_value_display import install
        install()
    except Exception as exc:
        logger.warning("[RealWallet] SOL/USDT value display install deferred: %s", exc)
    finally:
        _installing_wallet_value_display = False


def _alphapulse_import(name, globals=None, locals=None, fromlist=(), level=0):
    module = _original_import(name, globals, locals, fromlist, level)
    try:
        _install_if_ready()
        _install_real_wallet_guard_if_ready()
        _install_wallet_value_display_if_ready()
    except Exception:
        # Import hooks must never break unrelated imports.
        pass
    return module


if not getattr(builtins.__import__, "_alphapulse_holder_hook", False):
    _alphapulse_import._alphapulse_holder_hook = True
    builtins.__import__ = _alphapulse_import

_install_if_ready()
_install_real_wallet_guard_if_ready()
_install_wallet_value_display_if_ready()
