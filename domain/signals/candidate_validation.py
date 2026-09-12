"""
Shared safety + snapshot pipeline reused by both new discovery engines
(Discovery Engine A: Wallet Consensus, Discovery Engine B: Robinhood
Chain Discovery).

Builds a candidate dict in EXACTLY the shape
domain.signals.pump_radar._build_pump_card_text() already expects
({contract, data, pump, security_data, holder_count, holder_analysis,
smart_money, whale_holders, real_lp_lock_pct, funding_clusters,
deployer_history, price_check, verified_warnings, confidence}), reusing
the same hard-reject / security / holder / scoring building blocks the
free Signal Engine and Premium Signal Engine already use
(domain.signals.scoring.hard_reject_reasons / score_candidate,
providers.marketdata.goplus, domain.intelligence.holders), so:

  * "Existing hard-reject conditions remain authoritative" (spec) —
    nothing here bypasses or re-implements a looser version of them.
  * Neither new engine forks a second card/snapshot implementation.

Solana-only enrichments that don't generalize to another chain
(holder concentration via Helius, LP-lock %, funding-cluster tracing,
deployer history, cross-provider price agreement) are simply omitted
(set to None/neutral) for non-Solana candidates -- every renderer in
_build_pump_card_text() already tolerates missing/None values for
these fields, so the shared card degrades gracefully rather than
guessing at chain-specific risk it cannot actually evaluate.

Both chains DO share one more enrichment: the King Token profile
(domain.intelligence.king_token_profile) -- a small, cached, bounded
scoring bonus for candidates that resemble this bot's own proven
multi-milestone winners. It is fetched once per validation call and
handed to score_candidate() on both paths below; see
domain.signals.scoring._king_pattern_bonus() for exactly how it's
used (additive-only, capped, never able to rescue a token that failed
a hard gate).
"""

from __future__ import annotations

import logging

from config.settings import ROBINHOOD_CHAIN_GOPLUS_ID, ROBINHOOD_REQUIRE_SECURITY_CHECK
from domain.intelligence.holders import get_holder_analysis
from domain.intelligence.king_token_profile import get_cached_king_profile
from domain.intelligence.sellability_check import verify_sellability
from domain.signals.scoring import hard_reject_reasons, score_candidate
from providers.marketdata.dexscreener import get_token_card_info
from providers.marketdata.goplus import check_token_security, check_token_security_for_chain

logger = logging.getLogger("WhaleAlpha.CandidateValidation")


async def build_validated_candidate(
    contract: str,
    *,
    chain: str = "solana",
    prefetched_data: dict | None = None,
) -> tuple[dict | None, list[str]]:
    """
    Runs the shared safety+snapshot pipeline for `contract` on `chain`.

    Returns (candidate, reject_reasons):
      * candidate is a dict ready for pump_radar._build_pump_card_text()
        (or pump_radar.send_pump_card()) when reject_reasons is empty.
      * candidate is None and reject_reasons is non-empty when the
        token must not be alerted on ("Existing hard-reject conditions
        remain authoritative" -- this function never overrides them).
    """
    king_profile = None
    try:
        king_profile = await get_cached_king_profile()
    except Exception as e:
        # Best-effort enrichment -- a King-profile lookup failure must
        # never block validation; score_candidate() already treats
        # king_profile=None as "no bonus", same as any caller that
        # predates this enrichment.
        logger.warning(f"King token profile lookup failed for {contract}: {e}")

    if chain == "solana":
        data = prefetched_data or await get_token_card_info(contract, "solana")
        if not data:
            return None, ["no_market_data"]

        sec = await check_token_security(contract)
        dev_address = (sec or {}).get("creator_address")
        holder_analysis = await get_holder_analysis(contract, dev_address=dev_address)
        holders = holder_analysis.get("total_holders") if holder_analysis else None

        reasons = hard_reject_reasons(data, sec, holder_analysis, contract)
        if reasons:
            return None, reasons

        # Production Risk Layer: live buy->sell Jupiter simulation. This is
        # a hard gate, same authority as hard_reject_reasons() above --
        # a token that cannot be verifiably sold back to SOL right now
        # must never reach the alert worker or be shown as tradable,
        # no matter how it scores otherwise. See
        # domain/intelligence/sellability_check.py for what this checks
        # and why it is fail-closed by default.
        sellability = await verify_sellability(contract, chain="solana")
        if sellability["reject"]:
            logger.info(f"Rejected {contract[:8]}: sellability={sellability['reasons']}")
            return None, sellability["reasons"] or ["sellability_unverified"]

        pump = score_candidate(data, sec, holder_analysis, holders, contract, king_profile=king_profile)

        candidate = {
            "contract": contract,
            "data": data,
            "pump": pump,
            "security_data": sec,
            "holder_count": holders,
            "holder_analysis": holder_analysis,
            "smart_money": None,
            "whale_holders": None,
            "real_lp_lock_pct": None,
            "funding_clusters": None,
            "deployer_history": None,
            "price_check": None,
            "verified_warnings": list(sellability.get("warnings", [])),
            "sellability": sellability,
            "confidence": {
                "confidence_score": 100 if sec else 50,
                "confirmed_count": 1 if sec else 0,
                "checked_count": 1,
            },
        }
        return candidate, []

    # --- non-Solana chain (Discovery Engine B: Robinhood Chain) ---
    data = prefetched_data or await get_token_card_info(contract, chain)
    if not data:
        return None, ["no_market_data"]

    sec = None
    if ROBINHOOD_CHAIN_GOPLUS_ID:
        sec = await check_token_security_for_chain(contract, ROBINHOOD_CHAIN_GOPLUS_ID)

    if sec is None and ROBINHOOD_REQUIRE_SECURITY_CHECK:
        # Fail closed, same convention the Solana path already follows
        # when GoPlus has nothing to say about a token.
        return None, ["security_data_unavailable"]

    reasons = hard_reject_reasons(data, sec, None, contract)
    if reasons:
        return None, reasons

    # Production Risk Layer: same hard gate as the Solana path above.
    # Jupiter only routes Solana, so for this chain verify_sellability()
    # returns a non-blocking neutral result today -- calling it here
    # unconditionally means this path picks up sellability simulation
    # automatically if/when it's ever extended to a chain Jupiter (or a
    # future equivalent) can route.
    sellability = await verify_sellability(contract, chain=chain)
    if sellability["reject"]:
        logger.info(f"Rejected {contract[:8]}: sellability={sellability['reasons']}")
        return None, sellability["reasons"] or ["sellability_unverified"]

    pump = score_candidate(data, sec, None, None, contract, king_profile=king_profile)

    candidate = {
        "contract": contract,
        "data": data,
        "pump": pump,
        "security_data": sec,
        "holder_count": None,
        "holder_analysis": None,
        "smart_money": None,
        "whale_holders": None,
        "real_lp_lock_pct": None,
        "funding_clusters": None,
        "deployer_history": None,
        "price_check": None,
        "verified_warnings": list(sellability.get("warnings", [])),
        "sellability": sellability,
        "confidence": {
            "confidence_score": 100 if sec else 40,
            "confirmed_count": 1 if sec else 0,
            "checked_count": 1,
        },
    }
    return candidate, []
