from domain.intelligence.holder_state import (
    INVALID_HOLDER_RESPONSE,
    PROVIDER_ERROR,
    UNAVAILABLE_HOLDER_DATA,
    VALID_HOLDER_DATA,
    normalize_holder_analysis,
)
from domain.signals.scoring import hard_reject_reasons, _score_holder_distribution


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


def test_provider_error_becomes_unknown_not_rejection():
    holder = normalize_holder_analysis(None)
    assert holder["holder_analysis_status"] == PROVIDER_ERROR
    assert holder["total_holders"] is None
    assert holder["top_holder_pct"] is None
    assert hard_reject_reasons(_valid_market_data(), _clean_security(), holder, "Examplepump") == []


def test_empty_holder_state_has_no_fabricated_concentration():
    holder = normalize_holder_analysis({
        "holder_analysis_status": UNAVAILABLE_HOLDER_DATA,
        "total_holders": None,
        "top_holder_pct": None,
        "top10_pct": None,
        "top25_pct": None,
        "dev_holding_pct": None,
        "bundle_wallet_count": 0,
        "bundle_pct": 0.0,
        "top_holder_addresses": [],
        "holder_data_truncated": False,
    })
    assert holder["top_holder_pct"] is None
    assert holder["top10_pct"] is None
    assert holder["bundle_pct"] == 0.0


def test_parser_drop_is_invalid_not_one_holder_or_100_percent():
    holder = normalize_holder_analysis({
        "holder_analysis_status": VALID_HOLDER_DATA,
        "total_holders": 1,
        "top_holder_pct": None,
        "top10_pct": None,
        "top25_pct": None,
        "dev_holding_pct": None,
        "bundle_wallet_count": 0,
        "bundle_pct": 0.0,
        "top_holder_addresses": [],
        "holder_data_truncated": False,
    })
    assert holder["holder_analysis_status"] == INVALID_HOLDER_RESPONSE
    assert holder["total_holders"] is None
    assert holder["top_holder_pct"] is None


def test_real_one_holder_still_rejects_concentration():
    holder = {
        "holder_analysis_status": VALID_HOLDER_DATA,
        "total_holders": 1,
        "top_holder_pct": 100.0,
        "top10_pct": 100.0,
        "top25_pct": 100.0,
        "dev_holding_pct": None,
        "bundle_wallet_count": 0,
        "bundle_pct": 0.0,
        "top_holder_addresses": ["real-owner"],
        "holder_data_truncated": False,
    }
    reasons = hard_reject_reasons(_valid_market_data(), _clean_security(), holder, "Examplepump")
    assert any("Single wallet holds 100.0%" in reason for reason in reasons)


def test_unknown_holder_evidence_can_use_existing_security_fallbacks_without_fake_holder_metrics():
    holder = normalize_holder_analysis(None)
    points, _ = _score_holder_distribution(
        holder,
        {"top_holder_percent": 5.0, "top_10_holder_percent": 20.0},
        1.0,
    )
    assert points > 0
    assert holder["top_holder_pct"] is None
    assert holder["top10_pct"] is None
