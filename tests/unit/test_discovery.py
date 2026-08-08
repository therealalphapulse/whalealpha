"""Unit tests for engines/discovery.py's pure decision functions — no DB,
no network. See tests/unit/test_discovery_metrics.py for metrics computation
and tests/unit/test_scoring.py for the underlying scoring algorithm itself.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from whale_alpha.engines.discovery import (
    DiscoveryConfig,
    DiscoveryFunnelStats,
    DiscoveryHistoryStats,
    _queue_new_candidates,
    decide_history_fetch_outcome,
    evaluate_promotion,
    evaluate_retention,
    select_wallets_to_retire_for_ceiling,
    start_discovery_loop,
)
from whale_alpha.engines.scoring import MIN_APPROVED_SCORE, WalletMetrics
from whale_alpha.integrations.wallet_discovery_source import DiscoveredCandidate, WalletHistoryFetch

NOW = datetime(2026, 8, 1, tzinfo=UTC)

CONFIG = DiscoveryConfig(
    min_tracked_wallets=500,
    max_tracked_wallets=1500,
    min_score_to_approve=55,
    min_roi_30d=0.15,
    min_win_rate=0.5,
    min_trade_count_30d=10,
    min_wallet_age_days=14,
    inactivity_timeout_days=21,
    low_score_cycles_before_retire=3,
)

QUALIFIED_METRICS = WalletMetrics(
    roi_30d=0.8,
    win_rate=0.75,
    pnl_usd_30d=50000,
    avg_hold_minutes=240,
    avg_position_usd=4000,
    trade_frequency_7d=15,
    wallet_age_days=300,
    max_drawdown=0.15,
    trade_success_rate=0.7,
)


# --------------------------------------------------------------------------
# evaluate_promotion
# --------------------------------------------------------------------------


def test_promotes_a_wallet_that_clears_every_gate():
    decision = evaluate_promotion(
        score=80, trade_count_30d=20, metrics=QUALIFIED_METRICS, flags=[], config=CONFIG
    )
    assert decision.approved
    assert decision.reason is None


def test_rejects_a_wallet_flagged_for_suspected_wash_trading_regardless_of_score():
    decision = evaluate_promotion(
        score=95,
        trade_count_30d=20,
        metrics=QUALIFIED_METRICS,
        flags=["SUSPECTED_WASH_TRADING_FREQUENCY"],
        config=CONFIG,
    )
    assert not decision.approved
    assert decision.reason == "SUSPECTED_WASH_TRADING"


def test_rejects_a_wallet_younger_than_the_minimum_age():
    young = WalletMetrics(**{**QUALIFIED_METRICS.__dict__, "wallet_age_days": 2})
    decision = evaluate_promotion(score=80, trade_count_30d=20, metrics=young, flags=[], config=CONFIG)
    assert not decision.approved
    assert decision.reason == "WALLET_TOO_NEW"


def test_rejects_a_wallet_with_too_few_trades():
    decision = evaluate_promotion(
        score=80, trade_count_30d=3, metrics=QUALIFIED_METRICS, flags=[], config=CONFIG
    )
    assert not decision.approved
    assert decision.reason == "INSUFFICIENT_TRADE_HISTORY"


def test_rejects_a_wallet_below_minimum_win_rate():
    low_win_rate = WalletMetrics(**{**QUALIFIED_METRICS.__dict__, "win_rate": 0.2})
    decision = evaluate_promotion(
        score=80, trade_count_30d=20, metrics=low_win_rate, flags=[], config=CONFIG
    )
    assert not decision.approved
    assert decision.reason == "WIN_RATE_BELOW_MINIMUM"


def test_rejects_a_wallet_below_minimum_roi():
    low_roi = WalletMetrics(**{**QUALIFIED_METRICS.__dict__, "roi_30d": 0.01})
    decision = evaluate_promotion(score=80, trade_count_30d=20, metrics=low_roi, flags=[], config=CONFIG)
    assert not decision.approved
    assert decision.reason == "ROI_BELOW_MINIMUM"


def test_rejects_a_wallet_below_minimum_score_even_if_metrics_pass():
    decision = evaluate_promotion(
        score=10, trade_count_30d=20, metrics=QUALIFIED_METRICS, flags=[], config=CONFIG
    )
    assert not decision.approved
    assert decision.reason == "SCORE_BELOW_MINIMUM"


# --------------------------------------------------------------------------
# evaluate_retention
# --------------------------------------------------------------------------


def test_retires_a_wallet_inactive_past_the_timeout():
    decision = evaluate_retention(
        score=90,
        consecutive_low_score_cycles=0,
        last_active_at=NOW - timedelta(days=30),
        now=NOW,
        config=CONFIG,
    )
    assert decision.retire
    assert decision.reason == "INACTIVITY"


def test_does_not_retire_an_active_high_scoring_wallet():
    decision = evaluate_retention(
        score=90,
        consecutive_low_score_cycles=0,
        last_active_at=NOW - timedelta(days=1),
        now=NOW,
        config=CONFIG,
    )
    assert not decision.retire


def test_retires_after_enough_consecutive_low_score_cycles():
    # Cycle 1 and 2: below MIN_APPROVED_SCORE but not yet 3 in a row.
    d1 = evaluate_retention(
        score=MIN_APPROVED_SCORE - 1,
        consecutive_low_score_cycles=0,
        last_active_at=NOW,
        now=NOW,
        config=CONFIG,
    )
    assert not d1.retire
    assert d1.new_consecutive_low_score_cycles == 1

    d2 = evaluate_retention(
        score=MIN_APPROVED_SCORE - 1,
        consecutive_low_score_cycles=d1.new_consecutive_low_score_cycles,
        last_active_at=NOW,
        now=NOW,
        config=CONFIG,
    )
    assert not d2.retire
    assert d2.new_consecutive_low_score_cycles == 2

    d3 = evaluate_retention(
        score=MIN_APPROVED_SCORE - 1,
        consecutive_low_score_cycles=d2.new_consecutive_low_score_cycles,
        last_active_at=NOW,
        now=NOW,
        config=CONFIG,
    )
    assert d3.retire
    assert d3.reason == "SUSTAINED_LOW_SCORE"


def test_resets_low_score_streak_once_score_recovers():
    decision = evaluate_retention(
        score=90,
        consecutive_low_score_cycles=2,
        last_active_at=NOW,
        now=NOW,
        config=CONFIG,
    )
    assert not decision.retire
    assert decision.new_consecutive_low_score_cycles == 0


def test_low_score_retirement_suppressed_when_population_is_at_the_floor():
    decision = evaluate_retention(
        score=MIN_APPROVED_SCORE - 1,
        consecutive_low_score_cycles=5,  # would otherwise clearly retire
        last_active_at=NOW,
        now=NOW,
        config=CONFIG,
        allow_low_score_retirement=False,
    )
    assert not decision.retire
    assert decision.new_consecutive_low_score_cycles == 6


def test_inactivity_retirement_not_suppressed_even_at_the_floor():
    # A dormant wallet should still go even when we're short on wallets —
    # keeping dead weight never helps the shortage.
    decision = evaluate_retention(
        score=90,
        consecutive_low_score_cycles=0,
        last_active_at=NOW - timedelta(days=60),
        now=NOW,
        config=CONFIG,
        allow_low_score_retirement=False,
    )
    assert decision.retire
    assert decision.reason == "INACTIVITY"


def test_missing_score_leaves_streak_untouched_and_does_not_retire():
    decision = evaluate_retention(
        score=None,
        consecutive_low_score_cycles=2,
        last_active_at=NOW,
        now=NOW,
        config=CONFIG,
    )
    assert not decision.retire
    assert decision.new_consecutive_low_score_cycles == 2


# --------------------------------------------------------------------------
# select_wallets_to_retire_for_ceiling
# --------------------------------------------------------------------------


def test_selects_nothing_when_under_the_ceiling():
    approved = [("a", 90), ("b", 80)]
    assert select_wallets_to_retire_for_ceiling(approved, max_tracked=1500) == []


def test_selects_lowest_scoring_wallets_to_bring_population_to_the_ceiling():
    approved = [("a", 90), ("b", 10), ("c", 50), ("d", 5)]
    to_retire = select_wallets_to_retire_for_ceiling(approved, max_tracked=2)
    assert set(to_retire) == {"b", "d"}


def test_selects_exactly_the_surplus_count():
    approved = [(str(i), float(i)) for i in range(10)]
    to_retire = select_wallets_to_retire_for_ceiling(approved, max_tracked=7)
    assert len(to_retire) == 3
    assert set(to_retire) == {"0", "1", "2"}  # three lowest scores


# --------------------------------------------------------------------------
# _queue_new_candidates — dedup + budget logic behind both sourcing streams
# (signal-derived and the trending-token bootstrap that fixes cold start).
# --------------------------------------------------------------------------


class _FakeSession:
    """Records what would have been session.add()'d, without a real DB —
    enough to test the dedup/budget logic in isolation."""

    def __init__(self):
        self.added: list[DiscoveredCandidate] = []

    def add(self, obj):
        self.added.append(obj)


def _addrs(n: int) -> list[str]:
    from solders.pubkey import Pubkey

    return [str(Pubkey.new_unique()) for _ in range(n)]


def test_queues_new_candidates_up_to_budget():
    session = _FakeSession()
    addrs = _addrs(5)
    candidates = [DiscoveredCandidate(address=a, source="test") for a in addrs]
    found, queued = _queue_new_candidates(session, candidates, known_addresses=set(), budget=3)
    assert found == 5
    assert queued == 3
    assert len(session.added) == 3


def test_skips_candidates_already_known_tracked_or_queued():
    session = _FakeSession()
    addrs = _addrs(5)
    known = {addrs[0], addrs[2]}
    candidates = [DiscoveredCandidate(address=a, source="test") for a in addrs]
    found, queued = _queue_new_candidates(session, candidates, known_addresses=known, budget=10)
    assert found == 5
    assert queued == 3
    assert {c.address for c in session.added} == {addrs[1], addrs[3], addrs[4]}


def test_rejects_an_invalid_solana_address():
    session = _FakeSession()
    candidates = [DiscoveredCandidate(address="not-a-real-address", source="test")]
    found, queued = _queue_new_candidates(session, candidates, known_addresses=set(), budget=10)
    assert found == 1
    assert queued == 0
    assert session.added == []


class _FakeLoopEnv:
    """Minimal stand-in for Env — start_discovery_loop only reads these two
    fields directly; run_discovery_cycle itself is mocked out below so it
    never touches the rest of a real Env/session/http_client/RPC connection."""

    def __init__(self, *, startup_delay: float, interval: float) -> None:
        self.DISCOVERY_STARTUP_DELAY_SECONDS = startup_delay
        self.DISCOVERY_INTERVAL_SECONDS = interval


async def test_discovery_loop_runs_first_cycle_after_startup_delay_not_full_interval():
    """Regression test for the Phase 1 stabilization bug: the loop used to
    `sleep(DISCOVERY_INTERVAL_SECONDS)` *before* ever calling
    run_discovery_cycle, so with the real 900s default the engine produced
    zero discovery logs for the first 15 minutes of every process lifetime
    — indistinguishable from "never started" in the logs. It must now run
    its first cycle after the much shorter DISCOVERY_STARTUP_DELAY_SECONDS."""
    env = _FakeLoopEnv(startup_delay=0.01, interval=100)  # interval kept huge on purpose

    with patch(
        "whale_alpha.engines.discovery.run_discovery_cycle", new_callable=AsyncMock
    ) as mock_cycle:
        stop = start_discovery_loop(env, session_factory=object(), http_client=object(), solana_connection=object())
        try:
            # Long enough to clear the 0.01s startup delay, nowhere near the
            # 100s interval — so a call here proves the fix, not a fluke.
            await asyncio.sleep(0.1)
            assert mock_cycle.await_count == 1
        finally:
            await stop()


def test_mutates_known_addresses_so_a_second_stream_cannot_double_queue():
    session = _FakeSession()
    known: set[str] = set()
    addr = _addrs(1)[0]
    first_stream = [DiscoveredCandidate(address=addr, source="signal_derived")]
    _queue_new_candidates(session, first_stream, known_addresses=known, budget=10)
    assert addr in known

    second_stream = [DiscoveredCandidate(address=addr, source="trending_token_holder")]
    _, queued = _queue_new_candidates(session, second_stream, known_addresses=known, budget=10)
    assert queued == 0
    assert len(session.added) == 1  # only the first stream's copy was queued


# --------------------------------------------------------------------------
# DiscoveryFunnelStats / DiscoveryHistoryStats — per-cycle observability
# metrics (Helius 429-pressure production audit). No DB/network: pure
# dataclass + _queue_new_candidates logic only.
# --------------------------------------------------------------------------


def test_funnel_stats_distinguishes_already_known_from_duplicates_within_one_cycle():
    """already_known = existed in the DB before this cycle started;
    duplicates_removed = the SAME address returned by more than one source
    within this cycle. These must not be conflated — they mean different
    things operationally (see DiscoveryFunnelStats' docstring)."""
    session = _FakeSession()
    addrs = _addrs(3)
    pre_existing_wallet, new_addr, seen_twice = addrs
    known_addresses = {pre_existing_wallet}  # simulates a wallet already tracked before this cycle
    already_known_at_cycle_start = set(known_addresses)
    stats = DiscoveryFunnelStats()

    # Source 1: one already-known wallet resurfaces, one genuinely new one.
    source_one = [
        DiscoveredCandidate(address=pre_existing_wallet, source="blockchain_scan"),
        DiscoveredCandidate(address=new_addr, source="blockchain_scan"),
        DiscoveredCandidate(address=seen_twice, source="blockchain_scan"),
    ]
    _queue_new_candidates(
        session,
        source_one,
        known_addresses,
        budget=10,
        stats=stats,
        already_known_at_cycle_start=already_known_at_cycle_start,
    )

    # Source 2 (later in the same cycle): re-reports the same brand-new
    # wallet source 1 already queued this cycle — a genuine in-cycle dup,
    # not an "already known from the DB" case.
    source_two = [DiscoveredCandidate(address=seen_twice, source="trending_token_holder")]
    _queue_new_candidates(
        session,
        source_two,
        known_addresses,
        budget=10,
        stats=stats,
        already_known_at_cycle_start=already_known_at_cycle_start,
    )

    assert stats.candidates_raw == 4
    assert stats.already_known == 1  # pre_existing_wallet
    assert stats.duplicates_removed == 1  # seen_twice, on its second appearance
    assert stats.invalid_address == 0
    assert {c.address for c in session.added} == {new_addr, seen_twice}


def test_funnel_stats_counts_invalid_addresses_separately():
    session = _FakeSession()
    stats = DiscoveryFunnelStats()
    candidates = [DiscoveredCandidate(address="not-a-real-solana-address", source="blockchain_scan")]
    _queue_new_candidates(session, candidates, known_addresses=set(), budget=10, stats=stats)
    assert stats.candidates_raw == 1
    assert stats.invalid_address == 1
    assert stats.already_known == 0
    assert stats.duplicates_removed == 0


def test_history_stats_records_a_fresh_successful_helius_fetch():
    stats = DiscoveryHistoryStats()
    history = WalletHistoryFetch(swaps=[], transient=False, source="HELIUS", cache_hit=False)
    stats.record(history, helius_configured=True)
    assert stats.as_dict() == {
        "helius_requests": 1,
        "helius_429": 0,
        "helius_success": 1,
        "rpc_fallback": 0,
        "rpc_fallback_success": 0,
    }


def test_history_stats_records_a_429_and_subsequent_successful_rpc_fallback():
    stats = DiscoveryHistoryStats()
    history = WalletHistoryFetch(
        swaps=[object()],
        transient=False,
        source="RPC_FALLBACK",
        partial=True,
        cache_hit=False,
        helius_rate_limited=True,
        rpc_fallback_attempted=True,
    )
    stats.record(history, helius_configured=True)
    assert stats.as_dict() == {
        "helius_requests": 1,
        "helius_429": 1,
        "helius_success": 0,  # source is RPC_FALLBACK, not a Helius success
        "rpc_fallback": 1,
        "rpc_fallback_success": 1,
    }


def test_history_stats_a_cache_hit_does_not_count_as_a_fresh_helius_request():
    stats = DiscoveryHistoryStats()
    history = WalletHistoryFetch(swaps=[], transient=False, source="HELIUS", cache_hit=True)
    stats.record(history, helius_configured=True)
    assert stats.helius_requests == 0
    assert stats.helius_success == 0


def test_history_stats_without_helius_configured_never_counts_requests():
    stats = DiscoveryHistoryStats()
    history = WalletHistoryFetch(swaps=None, transient=False, source="HELIUS", cache_hit=False)
    stats.record(history, helius_configured=False)
    assert stats.helius_requests == 0


def test_history_stats_accumulate_across_multiple_record_calls():
    stats = DiscoveryHistoryStats()
    stats.record(
        WalletHistoryFetch(swaps=[], transient=False, source="HELIUS", cache_hit=False), helius_configured=True
    )
    stats.record(
        WalletHistoryFetch(swaps=None, transient=True, source="HELIUS", cache_hit=False, helius_rate_limited=True),
        helius_configured=True,
    )
    stats.record(
        WalletHistoryFetch(
            swaps=[object()],
            transient=False,
            source="RPC_FALLBACK",
            cache_hit=False,
            rpc_fallback_attempted=True,
        ),
        helius_configured=True,
    )
    assert stats.as_dict() == {
        "helius_requests": 3,
        "helius_429": 1,
        "helius_success": 1,
        "rpc_fallback": 1,
        "rpc_fallback_success": 1,
    }


# --------------------------------------------------------------------------
# decide_history_fetch_outcome — retry-queue decision logic (production fix:
# a candidate whose wallet-history fetch fails transiently, e.g. Helius
# returning 429, must be requeued for retry, never rejected outright).
# --------------------------------------------------------------------------

_RETRY_KWARGS = dict(
    now=NOW,
    max_retries_before_reject=5,
    retry_base_seconds=1.0,
    retry_max_seconds=20.0,
)


def test_history_available_continues_regardless_of_retry_count():
    outcome = decide_history_fetch_outcome(
        swaps_available=True, transient=False, history_retry_count=3, **_RETRY_KWARGS
    )
    assert outcome.outcome == "CONTINUE"
    assert outcome.new_retry_count == 3  # untouched


def test_transient_failure_under_budget_is_queued_for_retry_not_rejected():
    outcome = decide_history_fetch_outcome(
        swaps_available=False, transient=True, history_retry_count=0, **_RETRY_KWARGS
    )
    assert outcome.outcome == "RETRY_QUEUED"
    assert outcome.new_retry_count == 1
    assert outcome.next_retry_at is not None
    assert outcome.next_retry_at > NOW


def test_retry_backoff_grows_exponentially_with_retry_count():
    first = decide_history_fetch_outcome(
        swaps_available=False, transient=True, history_retry_count=0, **_RETRY_KWARGS
    )
    second = decide_history_fetch_outcome(
        swaps_available=False, transient=True, history_retry_count=1, **_RETRY_KWARGS
    )
    assert (second.next_retry_at - NOW) > (first.next_retry_at - NOW)


def test_retry_backoff_is_capped():
    outcome = decide_history_fetch_outcome(
        swaps_available=False, transient=True, history_retry_count=50, **{**_RETRY_KWARGS, "max_retries_before_reject": 100}
    )
    assert (outcome.next_retry_at - NOW) <= timedelta(seconds=20.0 * 4)


def test_transient_failure_at_budget_is_permanently_rejected_not_retried_forever():
    outcome = decide_history_fetch_outcome(
        swaps_available=False, transient=True, history_retry_count=5, **_RETRY_KWARGS
    )
    assert outcome.outcome == "PERMANENT_REJECT"
    assert outcome.next_retry_at is None


def test_permanent_failure_is_rejected_immediately_even_with_full_retry_budget():
    """No HELIUS_API_KEY configured, or a definitive 4xx — retrying can
    never succeed, so this must never enter the retry queue regardless of
    history_retry_count."""
    outcome = decide_history_fetch_outcome(
        swaps_available=False, transient=False, history_retry_count=0, **_RETRY_KWARGS
    )
    assert outcome.outcome == "PERMANENT_REJECT"
