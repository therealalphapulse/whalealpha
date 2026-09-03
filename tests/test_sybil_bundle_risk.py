"""
tests/test_sybil_bundle_risk.py

Phase 3.1 regression coverage: balance-similarity ("bundle") clustering
must be treated as evidence, not proof, of Sybil/coordinated ownership.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domain.signals.scoring import (
    BUNDLE_MODERATE_PCT,
    BUNDLE_SEVERE_PCT,
    BUNDLE_SEVERE_PCT_TRUNCATED,
    evaluate_sybil_bundle_risk,
    hard_reject_reasons,
)


def _clean_security():
    return {
        "is_honeypot": "0",
        "cannot_sell_all": "0",
        "cannot_buy": "0",
        "is_blacklisted": "0",
        "hidden_owner": "0",
        "mintable": "0",
        "freezable": "0",
    }


def _valid_market_data():
    return {"liquidity": 10000, "market_cap": 50000}


def _holder(bundle_pct, truncated=False, **extra):
    base = {
        "bundle_pct": bundle_pct,
        "bundle_wallet_count": 5,
        "holder_data_truncated": truncated,
        "top_holder_pct": 5.0,
        "top10_pct": 20.0,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------
# evaluate_sybil_bundle_risk: tiering
# ---------------------------------------------------------------------
def test_balance_similarity_alone_does_not_prove_sybil_below_moderate():
    evidence = evaluate_sybil_bundle_risk(_holder(15.0))
    assert evidence["balance_similarity_evidence"] is False
    assert evidence["hard_reject"] is False
    assert evidence["coordination_confidence"] == "none"


def test_moderate_bundle_percentage_does_not_automatically_hard_reject():
    evidence = evaluate_sybil_bundle_risk(_holder(BUNDLE_MODERATE_PCT + 5))
    assert evidence["balance_similarity_evidence"] is True
    assert evidence["hard_reject"] is False
    assert evidence["coordination_confidence"] == "moderate"
    assert evidence["reasons"] == []


def test_severe_concentration_still_rejects():
    evidence = evaluate_sybil_bundle_risk(_holder(BUNDLE_SEVERE_PCT + 1))
    assert evidence["hard_reject"] is True
    assert evidence["coordination_confidence"] == "severe"
    assert evidence["reasons"]
    assert "concentration risk" in evidence["reasons"][0]


def test_truncated_holder_data_raises_the_severe_bar():
    # Between the normal severe threshold and the truncated threshold,
    # a truncated snapshot must NOT hard-reject.
    midpoint = (BUNDLE_SEVERE_PCT + BUNDLE_SEVERE_PCT_TRUNCATED) / 2
    evidence_truncated = evaluate_sybil_bundle_risk(_holder(midpoint, truncated=True))
    evidence_not_truncated = evaluate_sybil_bundle_risk(_holder(midpoint, truncated=False))
    assert evidence_truncated["hard_reject"] is False
    assert evidence_not_truncated["hard_reject"] is True


def test_legitimate_distributed_holders_remain_valid():
    evidence = evaluate_sybil_bundle_risk(_holder(0.0))
    assert evidence["hard_reject"] is False
    assert evidence["balance_similarity_evidence"] is False
    assert evidence["coordination_confidence"] == "none"


# ---------------------------------------------------------------------
# hard_reject_reasons integration
# ---------------------------------------------------------------------
def test_hard_reject_reasons_does_not_reject_moderate_bundle():
    reasons = hard_reject_reasons(
        _valid_market_data(), _clean_security(), _holder(45.0), "Examplepump"
    )
    assert not any("cluster" in r.lower() or "bundle" in r.lower() for r in reasons)


def test_hard_reject_reasons_rejects_severe_bundle():
    reasons = hard_reject_reasons(
        _valid_market_data(), _clean_security(), _holder(BUNDLE_SEVERE_PCT + 5), "Examplepump"
    )
    assert any("concentration risk" in r for r in reasons)


def test_hard_reject_reasons_severe_bundle_with_truncation_is_not_auto_rejected():
    # Severe by the non-truncated threshold, but data was truncated --
    # must not hard-reject on this alone.
    reasons = hard_reject_reasons(
        _valid_market_data(),
        _clean_security(),
        _holder(BUNDLE_SEVERE_PCT + 5, truncated=True),
        "Examplepump",
    )
    assert not any("concentration risk" in r for r in reasons)


def test_hard_reject_reasons_still_rejects_other_hard_conditions_independently():
    holder = _holder(0.0, top_holder_pct=99.0)
    reasons = hard_reject_reasons(_valid_market_data(), _clean_security(), holder, "Examplepump")
    assert any("Single wallet holds" in r for r in reasons)


def test_missing_bundle_pct_is_not_treated_as_severe():
    holder = _holder(0.0)
    holder.pop("bundle_pct")
    reasons = hard_reject_reasons(_valid_market_data(), _clean_security(), holder, "Examplepump")
    assert not any("concentration risk" in r for r in reasons)


if __name__ == "__main__":
    tests = [
        test_balance_similarity_alone_does_not_prove_sybil_below_moderate,
        test_moderate_bundle_percentage_does_not_automatically_hard_reject,
        test_severe_concentration_still_rejects,
        test_truncated_holder_data_raises_the_severe_bar,
        test_legitimate_distributed_holders_remain_valid,
        test_hard_reject_reasons_does_not_reject_moderate_bundle,
        test_hard_reject_reasons_rejects_severe_bundle,
        test_hard_reject_reasons_severe_bundle_with_truncation_is_not_auto_rejected,
        test_hard_reject_reasons_still_rejects_other_hard_conditions_independently,
        test_missing_bundle_pct_is_not_treated_as_severe,
    ]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
