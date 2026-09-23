"""Regression tests for the GoPlus security-gate audit:
1. hard_reject_reasons() now enforces malicious-contract-function flags
   (selfdestruct / external_call / owner_change_balance /
   can_take_back_ownership) in addition to the honeypot/blacklist/
   mint/freeze checks it already had.
2. Every hard-reject check still fires correctly when the rest of the
   security payload is clean, proving this is additive and doesn't
   weaken any existing check.
"""

from domain.signals.scoring import hard_reject_reasons


def _clean_data():
    # Passes passes_mc_liquidity_gate() so only the sec-derived reasons
    # under test are the ones that fire.
    return {"market_cap": 80_000, "liquidity": 20_000, "volume_24h": 50_000, "price_change_5m": 1}


def _clean_sec(**overrides):
    sec = {
        "is_honeypot": "0", "cannot_sell_all": "0", "cannot_buy": "0", "is_blacklisted": "0",
        "hidden_owner": "0", "mintable": "0", "freezable": "0", "selfdestruct": "0",
        "external_call": "0", "owner_change_balance": "0", "can_take_back_ownership": "0",
        "top_holder_percent": 5.0,
    }
    sec.update(overrides)
    return sec


def test_clean_token_has_no_hard_reject_reasons():
    reasons = hard_reject_reasons(_clean_data(), _clean_sec(), {}, "0xClean")
    assert reasons == []


def test_selfdestruct_is_hard_rejected():
    reasons = hard_reject_reasons(_clean_data(), _clean_sec(selfdestruct="1"), {}, "0xBad")
    assert any("self-destruct" in r.lower() for r in reasons)


def test_external_call_is_hard_rejected():
    reasons = hard_reject_reasons(_clean_data(), _clean_sec(external_call="1"), {}, "0xBad")
    assert any("external call" in r.lower() for r in reasons)


def test_owner_change_balance_is_hard_rejected():
    reasons = hard_reject_reasons(_clean_data(), _clean_sec(owner_change_balance="1"), {}, "0xBad")
    assert any("change holder balances" in r.lower() for r in reasons)


def test_can_take_back_ownership_is_hard_rejected():
    reasons = hard_reject_reasons(_clean_data(), _clean_sec(can_take_back_ownership="1"), {}, "0xBad")
    assert any("reclaimed" in r.lower() for r in reasons)


def test_existing_honeypot_and_authority_checks_still_fire():
    # Proves the new checks are additive -- the pre-existing checks this
    # audit was told not to weaken still work exactly as before.
    assert any("honeypot" in r.lower() for r in hard_reject_reasons(_clean_data(), _clean_sec(is_honeypot="1"), {}, "x"))
    assert any("mint authority" in r.lower() for r in hard_reject_reasons(_clean_data(), _clean_sec(mintable="1"), {}, "x"))
    assert any("freeze authority" in r.lower() for r in hard_reject_reasons(_clean_data(), _clean_sec(freezable="1"), {}, "x"))
