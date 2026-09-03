"""Deterministic startup wiring for production holder-data adapters.

The holder adapters are runtime compatibility layers around the existing
holder implementation. They must be installed after the holder module is
importable and before worker loops begin; relying only on import-hook timing
made production behavior dependent on import order.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("AlphaPulse.Holders")

_INSTALLED = False


def install() -> None:
    """Install the Helius V2 normalizer and indexed holder fallbacks exactly once.

    Fallback order (each wraps whatever holders._fetch_token_accounts
    currently is, so install order == provider try-order):
      Helius getProgramAccountsV2 / legacy getProgramAccounts (in holders.py)
        -> Solana Tracker (install_tracker)
        -> Birdeye (install_birdeye, Phase 3.2)
        -> Vybe (install_vybe)
        -> Codex (install_codex, final indexed holder fallback)

    Moralis and Shyft are intentionally NOT installed because they were
    returning unusable responses in production and adding latency/log noise.
    Nothing about those providers is changed here.

    Codex is appended at the END of the remaining chain. Nothing about the
    existing providers' behavior or order changes.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from domain.intelligence._holder_v2_compat import install as install_v2
    from domain.intelligence._solana_tracker_holder_fallback import install as install_tracker
    from domain.intelligence._birdeye_holder_fallback import install as install_birdeye
    from domain.intelligence._vybe_holder_fallback import install as install_vybe
    from domain.intelligence._codex_holder_fallback import install as install_codex

    install_v2()
    install_tracker()
    install_birdeye()
    install_vybe()
    install_codex()
    _INSTALLED = True
    logger.info("[HolderDiag] Production holder adapters installed at worker startup")
