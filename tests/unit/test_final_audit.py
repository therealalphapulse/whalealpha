from __future__ import annotations

from datetime import UTC, datetime

from whale_alpha.engines.final_audit import _closed, _metric, _score, _tier
from whale_alpha.engines.reversal_hunter import Candle, FlowEvidence, OnChainEvidence, PatternEvidence


def test_closed_candles_exclude_open_five_minute_candle():
    now = 1_787_771_000
    candles = [Candle(now - 600, 1, 1, 1, 1, 10), Candle(now - 300, 1, 1, 1, 1, 10), Candle(now, 1, 1, 1, 1, 10)]
    closed = _closed(candles, now)
    assert [c.ts for c in closed] == [now - 600, now - 300]


def test_score_weights_sum_to_100():
    p = PatternEvidence(20, 12, 60, 5, True, 2, 2, 1)
    f = FlowEvidence(100, 300, 2, 2, 1.5, True, "SUPPORTIVE", "SUPPORTIVE", 180, 120)
    o = OnChainEvidence(10, 3, 1, 3, True, (), ())
    score = _score(p, f, o, 100, 100)
    assert 80 <= score <= 100
    assert _tier(score) in {"EARLY WATCH", "STRONG WATCH", "HIGH-CONVICTION WATCH"}


def test_metric_contains_required_provenance_fields():
    now = datetime.now(UTC)
    m = _metric("price_usd", 1.2, "USD", "DexScreener", "pair", "mint", "pair", "USDC", now, now, "direct", "FRESH")
    required = {"metric_name", "raw_value", "normalized_value", "unit", "source_name", "source_endpoint_or_category", "token_address", "pair_address", "quote_token", "observed_at_utc", "fetched_at_utc", "blockchain_slot", "calculation_method", "freshness_status", "validation_status"}
    assert required <= set(m)


def test_run_final_release_audit_has_no_unresolved_globals():
    """Regression test for the production NameError: run_final_release_audit
    crashed with `NameError: name '_fetch_trade_data' is not defined` because
    it was called but never imported from reversal_hunter. This inspects the
    function's bytecode for LOAD_GLOBAL references specifically (not
    LOAD_ATTR/LOAD_METHOD, which would false-positive on every attribute
    access like analysis.snapshot.mint) and checks each one resolves in the
    final_audit module namespace or builtins, so any similarly-missing
    import in this function is caught here, not in production."""
    import builtins
    import dis

    from whale_alpha.engines import final_audit

    func = final_audit.run_final_release_audit
    global_names = {
        instr.argval
        for instr in dis.get_instructions(func.__code__)
        if instr.opname == "LOAD_GLOBAL" and isinstance(instr.argval, str)
    }
    unresolved = [
        name for name in global_names
        if name not in final_audit.__dict__ and not hasattr(builtins, name)
    ]
    assert not unresolved, f"Unresolved global names in run_final_release_audit: {unresolved}"


def test_fetch_trade_data_imported_from_reversal_hunter():
    """_fetch_trade_data must be the same approved function reversal_hunter
    already defines and every sibling _fetch_* call already uses -- not a
    new/duplicated implementation."""
    from whale_alpha.engines import final_audit, reversal_hunter

    assert final_audit._fetch_trade_data is reversal_hunter._fetch_trade_data
