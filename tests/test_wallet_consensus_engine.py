"""
Tests for Discovery Engine A (Solana Profitable Wallet Consensus).

Covers WhaleAlpha spec test requirements 1-7, 12-15:
  1. wallet profitability qualification
  2. wallet deduplication
  3. multiple buys by same wallet count as one
  4. 2 wallets do NOT trigger consensus
  5. 3 distinct profitable wallets DO trigger consensus
  6. wallets outside observation window do not count
  7. consensus evidence is persisted
  12. signal deduplication
  13. cooldown/re-arm
  14. safety rejection still blocks alert
  15. provider failure is handled gracefully
"""

import os
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/test")

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from domain.signals.wallet_consensus_engine import (
    _wallet_qualifies,
    _group_consensus_candidates,
    _cooldown_active,
    run_wallet_consensus_cycle,
)
from models.wallet_consensus_signal import WalletConsensusSignal


def _now():
    return datetime.now(timezone.utc)


@dataclass
class FakeWallet:
    wallet_address: str
    status: str = "active"
    reputation_score: float = 90.0
    classification: str = "profitable_trader,smart_money"


@dataclass
class FakeTrade:
    token_mint: str
    wallet_id: int = 1
    token_symbol: str = "FOO"
    side: str = "buy"
    signature: str = "sig"
    amount: float = 100.0
    value_usd_at_detection: float = 500.0
    detected_at: datetime = field(default_factory=_now)


# ---------------------------------------------------------------------
# 1. Wallet profitability qualification
# ---------------------------------------------------------------------

def test_wallet_qualifies_when_active_scored_and_profitable():
    w = FakeWallet("A", status="active", reputation_score=95, classification="profitable_trader")
    assert _wallet_qualifies(w) is True


def test_wallet_does_not_qualify_when_score_too_low():
    w = FakeWallet("A", status="active", reputation_score=0, classification="profitable_trader")
    assert _wallet_qualifies(w) is False


def test_wallet_does_not_qualify_when_not_active():
    w = FakeWallet("A", status="watch", reputation_score=95, classification="profitable_trader")
    assert _wallet_qualifies(w) is False


def test_wallet_does_not_qualify_without_profitable_classification():
    w = FakeWallet("A", status="active", reputation_score=95, classification="smart_money")
    assert _wallet_qualifies(w) is False


# ---------------------------------------------------------------------
# 2 & 3. Wallet dedup / multiple buys by the same wallet count as one
# ---------------------------------------------------------------------

def test_multiple_buys_by_same_wallet_count_once():
    wallet_a = FakeWallet("WALLET_A")
    rows = [
        (FakeTrade("TOKEN_X", signature="sig1"), wallet_a),
        (FakeTrade("TOKEN_X", signature="sig2"), wallet_a),
        (FakeTrade("TOKEN_X", signature="sig3"), wallet_a),
    ]
    window_start = _now() - timedelta(hours=1)
    candidates = _group_consensus_candidates(rows, window_start=window_start, min_wallets=1)
    assert len(candidates) == 1
    assert candidates[0]["mint"] == "TOKEN_X"
    assert len(candidates[0]["contributors"]) == 1  # deduped to exactly one wallet


# ---------------------------------------------------------------------
# 4. Two wallets do NOT trigger consensus (default threshold is 3)
# ---------------------------------------------------------------------

def test_two_distinct_wallets_do_not_trigger_consensus():
    rows = [
        (FakeTrade("TOKEN_X"), FakeWallet("WALLET_A")),
        (FakeTrade("TOKEN_X"), FakeWallet("WALLET_B")),
    ]
    window_start = _now() - timedelta(hours=1)
    candidates = _group_consensus_candidates(rows, window_start=window_start, min_wallets=3)
    assert candidates == []


# ---------------------------------------------------------------------
# 5. Three distinct profitable wallets DO trigger consensus
# ---------------------------------------------------------------------

def test_three_distinct_wallets_trigger_consensus():
    rows = [
        (FakeTrade("TOKEN_X"), FakeWallet("WALLET_A")),
        (FakeTrade("TOKEN_X"), FakeWallet("WALLET_B")),
        (FakeTrade("TOKEN_X"), FakeWallet("WALLET_C")),
    ]
    window_start = _now() - timedelta(hours=1)
    candidates = _group_consensus_candidates(rows, window_start=window_start, min_wallets=3)
    assert len(candidates) == 1
    assert len(candidates[0]["contributors"]) == 3


def test_twenty_buys_from_one_wallet_never_reaches_three_wallet_threshold():
    wallet_a = FakeWallet("WALLET_A")
    rows = [(FakeTrade("TOKEN_X", signature=f"sig{i}"), wallet_a) for i in range(20)]
    window_start = _now() - timedelta(hours=1)
    candidates = _group_consensus_candidates(rows, window_start=window_start, min_wallets=3)
    assert candidates == []  # still just 1 distinct wallet, per spec example


# ---------------------------------------------------------------------
# 6. Wallets outside the observation window do not count
# ---------------------------------------------------------------------

def test_purchases_outside_window_do_not_count():
    now = _now()
    rows = [
        (FakeTrade("TOKEN_X", detected_at=now - timedelta(minutes=5)), FakeWallet("WALLET_A")),
        (FakeTrade("TOKEN_X", detected_at=now - timedelta(minutes=10)), FakeWallet("WALLET_B")),
        # This one is well outside a 60-minute window -- must not count.
        (FakeTrade("TOKEN_X", detected_at=now - timedelta(hours=5)), FakeWallet("WALLET_C")),
    ]
    window_start = now - timedelta(minutes=60)
    candidates = _group_consensus_candidates(rows, window_start=window_start, min_wallets=3)
    assert candidates == []  # only 2 distinct wallets are actually within the window


# ---------------------------------------------------------------------
# 12/13. Signal dedupe + cooldown/re-arm
# ---------------------------------------------------------------------

def test_cooldown_active_true_when_not_expired():
    row = SimpleNamespace(cooldown_expires_at=_now() + timedelta(hours=1))
    assert _cooldown_active(row) is True


def test_cooldown_active_false_when_expired():
    row = SimpleNamespace(cooldown_expires_at=_now() - timedelta(hours=1))
    assert _cooldown_active(row) is False


def test_cooldown_active_false_when_no_row():
    assert _cooldown_active(None) is False


# ---------------------------------------------------------------------
# 7. Consensus evidence is persisted / 14. safety rejection blocks alert
# / 15. provider failure is handled gracefully -- exercised together
#   via run_wallet_consensus_cycle() with everything mocked out.
# ---------------------------------------------------------------------

class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Minimal async-context-manager session stub. `execute` is patched
    per-test to return whatever the test needs."""

    def __init__(self, existing_signal=None):
        self.existing_signal = existing_signal
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **kw):
        return _FakeResult(self.existing_signal)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_consensus_evidence_is_persisted_and_signal_sent():
    wallets = {
        "WALLET_A": (FakeTrade("MINT1", signature="s1"), FakeWallet("WALLET_A")),
        "WALLET_B": (FakeTrade("MINT1", signature="s2"), FakeWallet("WALLET_B")),
        "WALLET_C": (FakeTrade("MINT1", signature="s3"), FakeWallet("WALLET_C")),
    }
    fake_candidate = {
        "mint": "MINT1",
        "token_symbol": "FOO",
        "contributors": wallets,
        "avg_reputation": 90.0,
    }
    fake_card = {
        "contract": "MINT1",
        "data": {"symbol": "FOO", "name": "Foo Token"},
        "pump": {"final_score": 80.0},
    }
    session = _FakeSession(existing_signal=None)

    bot = AsyncMock()

    with patch("domain.signals.wallet_consensus_engine.async_session", return_value=session), \
         patch("domain.signals.wallet_consensus_engine.run_monitor_cycle", new=AsyncMock(return_value={"trades_recorded": 3})), \
         patch("domain.signals.wallet_consensus_engine.find_wallet_consensus_candidates", new=AsyncMock(return_value=[fake_candidate])), \
         patch("domain.signals.wallet_consensus_engine.build_validated_candidate", new=AsyncMock(return_value=(fake_card, []))), \
         patch("domain.signals.wallet_consensus_engine.get_funding_clusters", new=AsyncMock(return_value={"clusters": [], "largest_cluster_size": 0, "traced": 0})), \
         patch("domain.signals.wallet_consensus_engine.load_channel_ids", return_value=[123456]), \
         patch("domain.signals.wallet_consensus_engine.send_pump_card", new=AsyncMock()) as mock_send:

        stats = await run_wallet_consensus_cycle(bot)

    assert stats["signals_sent"] == 1
    assert stats["safety_rejected"] == 0
    assert session.committed is True
    assert len(session.added) == 1

    persisted = session.added[0]
    assert isinstance(persisted, WalletConsensusSignal)
    assert persisted.wallet_count == 3
    assert set(json.loads(persisted.wallet_addresses_json)) == {"WALLET_A", "WALLET_B", "WALLET_C"}
    evidence = json.loads(persisted.transaction_evidence_json)
    assert len(evidence) == 3
    assert {"WALLET_A", "WALLET_B", "WALLET_C"} == {e["wallet"] for e in evidence}
    assert persisted.cooldown_expires_at is not None

    mock_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_safety_rejection_still_blocks_alert():
    """14. Existing hard-reject conditions remain authoritative --
    a consensus candidate that fails validation must never be sent or
    persisted as an active signal."""
    wallets = {
        "WALLET_A": (FakeTrade("MINT2"), FakeWallet("WALLET_A")),
        "WALLET_B": (FakeTrade("MINT2"), FakeWallet("WALLET_B")),
        "WALLET_C": (FakeTrade("MINT2"), FakeWallet("WALLET_C")),
    }
    fake_candidate = {"mint": "MINT2", "token_symbol": "BAR", "contributors": wallets, "avg_reputation": 85.0}
    session = _FakeSession(existing_signal=None)
    bot = AsyncMock()

    with patch("domain.signals.wallet_consensus_engine.async_session", return_value=session), \
         patch("domain.signals.wallet_consensus_engine.run_monitor_cycle", new=AsyncMock(return_value={"trades_recorded": 0})), \
         patch("domain.signals.wallet_consensus_engine.find_wallet_consensus_candidates", new=AsyncMock(return_value=[fake_candidate])), \
         patch("domain.signals.wallet_consensus_engine.build_validated_candidate", new=AsyncMock(return_value=(None, ["Honeypot"]))), \
         patch("domain.signals.wallet_consensus_engine.load_channel_ids", return_value=[123456]), \
         patch("domain.signals.wallet_consensus_engine.send_pump_card", new=AsyncMock()) as mock_send:

        stats = await run_wallet_consensus_cycle(bot)

    assert stats["safety_rejected"] == 1
    assert stats["signals_sent"] == 0
    assert session.added == []
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_skips_already_alerted_token():
    """13. cooldown/re-arm: a token still inside its cooldown window is
    skipped, not re-alerted."""
    wallets = {
        "WALLET_A": (FakeTrade("MINT3"), FakeWallet("WALLET_A")),
        "WALLET_B": (FakeTrade("MINT3"), FakeWallet("WALLET_B")),
        "WALLET_C": (FakeTrade("MINT3"), FakeWallet("WALLET_C")),
    }
    fake_candidate = {"mint": "MINT3", "token_symbol": "BAZ", "contributors": wallets, "avg_reputation": 85.0}
    existing_signal = SimpleNamespace(cooldown_expires_at=_now() + timedelta(hours=5))
    session = _FakeSession(existing_signal=existing_signal)
    bot = AsyncMock()

    with patch("domain.signals.wallet_consensus_engine.async_session", return_value=session), \
         patch("domain.signals.wallet_consensus_engine.run_monitor_cycle", new=AsyncMock(return_value={"trades_recorded": 0})), \
         patch("domain.signals.wallet_consensus_engine.find_wallet_consensus_candidates", new=AsyncMock(return_value=[fake_candidate])), \
         patch("domain.signals.wallet_consensus_engine.send_pump_card", new=AsyncMock()) as mock_send:

        stats = await run_wallet_consensus_cycle(bot)

    assert stats["cooldown_skipped"] == 1
    assert stats["signals_sent"] == 0
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_failure_in_purchase_tracking_is_handled_gracefully():
    """15. Provider failure (e.g. Helius/monitor cycle raising) must not
    crash the whole cycle -- consensus detection should still proceed
    against whatever evidence already exists."""
    session = _FakeSession(existing_signal=None)
    bot = AsyncMock()

    with patch("domain.signals.wallet_consensus_engine.async_session", return_value=session), \
         patch("domain.signals.wallet_consensus_engine.run_monitor_cycle", new=AsyncMock(side_effect=RuntimeError("Helius down"))), \
         patch("domain.signals.wallet_consensus_engine.find_wallet_consensus_candidates", new=AsyncMock(return_value=[])):

        stats = await run_wallet_consensus_cycle(bot)  # must not raise

    assert stats["purchases_observed"] == 0
    assert stats["consensus_candidates"] == 0
