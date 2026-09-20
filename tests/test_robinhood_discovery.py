"""
Tests for Discovery Engine B (Robinhood Chain Token Discovery via
DexScreener).

Covers WhaleAlpha spec test requirements 8-11, 13, 14, 15:
  8.  Robinhood tokens are filtered to Robinhood Chain
  9.  new and old Robinhood tokens can be discovered
  10. Robinhood scoring works
  11. Robinhood signal does not require wallet consensus
  13. cooldown/re-arm
  14. safety rejection still blocks alert
  15. provider failure is handled gracefully
"""

import os
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/test")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from domain.signals.robinhood_discovery import (
    discover_candidates,
    _passes_activity_filters,
    score_fresh_potential as score_potential,
    _cooldown_active,
    run_robinhood_discovery_cycle,
)
from models.robinhood_discovery_signal import RobinhoodDiscoverySignal


def _now():
    return datetime.now(timezone.utc)


def _pair(chain_id="robinhood", **overrides):
    base = {
        "chainId": chain_id,
        "tokenAddress": overrides.pop("tokenAddress", "TOKEN123"),
        "address": overrides.pop("tokenAddress2", "TOKEN123"),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------
# 8. Robinhood tokens are filtered to Robinhood Chain
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discovery_filters_out_non_robinhood_chain_tokens():
    profiles = [
        {"chainId": "robinhood", "tokenAddress": "RH_TOKEN"},
        {"chainId": "ethereum", "tokenAddress": "ETH_TOKEN"},
        {"chainId": "solana", "tokenAddress": "SOL_TOKEN"},
    ]
    with patch("domain.signals.robinhood_discovery.get_latest_token_profiles", new=AsyncMock(return_value=profiles)), \
         patch("domain.signals.robinhood_discovery.get_latest_boosted_tokens", new=AsyncMock(return_value=[])), \
         patch("domain.signals.robinhood_discovery.search_pairs_by_chain", new=AsyncMock(return_value=[])):
        candidates = await discover_candidates()

    contracts = {c["contract"] for c in candidates}
    assert contracts == {"RH_TOKEN"}


# ---------------------------------------------------------------------
# 9. New and old (renewed activity) Robinhood tokens can be discovered
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discovers_both_new_and_renewed_tokens():
    new_profile = [{"chainId": "robinhood", "tokenAddress": "NEW_TOKEN"}]
    old_pair_created_ms = (_now() - timedelta(hours=200)).timestamp() * 1000
    renewed_pairs = [
        {
            "contract": "OLD_TOKEN",
            "pair_created": old_pair_created_ms,
            "volume_1h": 20000,   # sharp acceleration vs 24h average
            "volume_24h": 24000,  # avg hourly = 1000, so 1h/avg = 20x
        }
    ]
    with patch("domain.signals.robinhood_discovery.get_latest_token_profiles", new=AsyncMock(return_value=new_profile)), \
         patch("domain.signals.robinhood_discovery.get_latest_boosted_tokens", new=AsyncMock(return_value=[])), \
         patch("domain.signals.robinhood_discovery.search_pairs_by_chain", new=AsyncMock(return_value=renewed_pairs)):
        candidates = await discover_candidates()

    by_contract = {c["contract"]: c for c in candidates}
    assert by_contract["NEW_TOKEN"]["source"] == "dexscreener_new"
    assert by_contract["OLD_TOKEN"]["source"] == "dexscreener_renewed"


@pytest.mark.asyncio
async def test_renewed_bucket_excludes_tokens_without_volume_acceleration():
    old_pair_created_ms = (_now() - timedelta(hours=200)).timestamp() * 1000
    renewed_pairs = [
        {
            "contract": "STALE_TOKEN",
            "pair_created": old_pair_created_ms,
            "volume_1h": 1000,
            "volume_24h": 24000,  # avg hourly = 1000, 1h/avg = 1.0x -- no acceleration
        }
    ]
    with patch("domain.signals.robinhood_discovery.get_latest_token_profiles", new=AsyncMock(return_value=[])), \
         patch("domain.signals.robinhood_discovery.get_latest_boosted_tokens", new=AsyncMock(return_value=[])), \
         patch("domain.signals.robinhood_discovery.search_pairs_by_chain", new=AsyncMock(return_value=renewed_pairs)):
        candidates = await discover_candidates()

    assert candidates == []


# ---------------------------------------------------------------------
# 10. Robinhood scoring works (configurable, never raw-volume-only)
# ---------------------------------------------------------------------

def test_score_potential_rewards_strong_activity():
    strong = {
        "liquidity": 200000, "price_change_1h": 20,
        "volume_1h": 100000, "volume_24h": 240000,
        "txns_1h_buys": 300, "txns_1h_sells": 100,
        "txns_24h_buys": 1200, "txns_24h_sells": 800,
    }
    weak = {
        "liquidity": 5000, "price_change_1h": -5,
        "volume_1h": 500, "volume_24h": 12000,
        "txns_1h_buys": 2, "txns_1h_sells": 5,
        "txns_24h_buys": 40, "txns_24h_sells": 60,
    }
    strong_score, breakdown = score_potential(strong)
    weak_score, _ = score_potential(weak)
    assert strong_score > weak_score
    assert set(breakdown.keys()) == {
        "liquidity", "volume", "volume_acceleration",
        "buy_sell_pressure", "tx_acceleration", "momentum_quality",
    }


def test_score_potential_is_not_driven_by_volume_alone():
    """Raw volume alone must never be sufficient -- a token with huge
    volume but weak liquidity/pressure/momentum should not automatically
    outscore a more balanced, moderate-volume token."""
    huge_volume_only = {
        "liquidity": 1000, "price_change_1h": -30,
        "volume_1h": 500000, "volume_24h": 500000,
        "txns_1h_buys": 5, "txns_1h_sells": 95,
        "txns_24h_buys": 100, "txns_24h_sells": 2000,
    }
    balanced = {
        "liquidity": 80000, "price_change_1h": 10,
        "volume_1h": 15000, "volume_24h": 100000,
        "txns_1h_buys": 60, "txns_1h_sells": 40,
        "txns_24h_buys": 600, "txns_24h_sells": 500,
    }
    huge_score, _ = score_potential(huge_volume_only)
    balanced_score, _ = score_potential(balanced)
    assert balanced_score > huge_score


def test_activity_filters_reject_illiquid_candidate():
    passes, reasons = _passes_activity_filters({
        "liquidity": 100, "volume_1h": 10, "txns_1h_buys": 0, "txns_1h_sells": 0, "market_cap": 1000000,
    })
    assert passes is False
    assert "liquidity_below_minimum" in reasons


def test_activity_filters_pass_healthy_candidate():
    passes, reasons = _passes_activity_filters({
        "liquidity": 50000, "volume_1h": 10000, "txns_1h_buys": 30, "txns_1h_sells": 20, "market_cap": 300000,
    })
    assert passes is True
    assert reasons == []


# ---------------------------------------------------------------------
# 13. cooldown/re-arm
# ---------------------------------------------------------------------

def test_cooldown_active_true_when_not_expired():
    row = SimpleNamespace(cooldown_expires_at=_now() + timedelta(hours=1))
    assert _cooldown_active(row) is True


def test_cooldown_active_false_when_no_row():
    assert _cooldown_active(None) is False


# ---------------------------------------------------------------------
# 11/14/15: full-cycle behavior with everything mocked out
# ---------------------------------------------------------------------

class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
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


HEALTHY_DATA = {
    "symbol": "RHUSD", "name": "Robinhood USD", "pool_address": "POOL1",
    "liquidity": 80000, "volume_1h": 15000, "volume_24h": 100000,
    "txns_1h_buys": 60, "txns_1h_sells": 40, "txns_24h_buys": 600, "txns_24h_sells": 500,
    "market_cap": 300000, "price_change_1h": 10,
}


@pytest.mark.asyncio
async def test_robinhood_signal_requires_no_wallet_consensus_and_persists_evidence():
    """11. A qualifying Robinhood candidate is signaled purely from
    DexScreener discovery + scoring + safety validation -- no wallet
    data of any kind is consulted anywhere in this cycle."""
    candidate_entry = {"contract": "RH1", "source": "dexscreener_new", "prefetched_data": HEALTHY_DATA}
    fake_card = {"contract": "RH1", "data": HEALTHY_DATA, "pump": {"final_score": 70.0}}
    session = _FakeSession(existing_signal=None)
    bot = AsyncMock()

    with patch("domain.signals.robinhood_discovery.discover_candidates", new=AsyncMock(return_value=[candidate_entry])), \
         patch("domain.signals.robinhood_discovery.async_session", return_value=session), \
         patch("domain.signals.robinhood_discovery.build_validated_candidate", new=AsyncMock(return_value=(fake_card, []))), \
         patch("domain.signals.robinhood_discovery.load_channel_ids", return_value=[999]), \
         patch("domain.signals.robinhood_discovery.send_pump_card", new=AsyncMock()) as mock_send:
        stats = await run_robinhood_discovery_cycle(bot)

    assert stats["signals_sent"] == 1
    assert stats["tokens_promoted"] == 1
    assert session.committed is True
    persisted = next((o for o in session.added if isinstance(o, RobinhoodDiscoverySignal)), None)
    assert persisted is not None
    assert persisted.token_contract == "RH1"
    assert persisted.discovery_source == "dexscreener_new:fresh"
    assert persisted.cooldown_expires_at is not None
    mock_send.assert_awaited_once()
    # No wallet-related import or object appears anywhere in this module.
    import domain.signals.robinhood_discovery as rh_mod
    assert not hasattr(rh_mod, "PremiumWallet")
    assert not hasattr(rh_mod, "WalletConsensusSignal")


@pytest.mark.asyncio
async def test_safety_rejection_still_blocks_robinhood_alert():
    """14. Existing hard-reject conditions remain authoritative for
    Robinhood Chain candidates too."""
    candidate_entry = {"contract": "RH2", "source": "dexscreener_new", "prefetched_data": HEALTHY_DATA}
    session = _FakeSession(existing_signal=None)
    bot = AsyncMock()

    with patch("domain.signals.robinhood_discovery.discover_candidates", new=AsyncMock(return_value=[candidate_entry])), \
         patch("domain.signals.robinhood_discovery.async_session", return_value=session), \
         patch("domain.signals.robinhood_discovery.build_validated_candidate", new=AsyncMock(return_value=(None, ["security_data_unavailable"]))), \
         patch("domain.signals.robinhood_discovery.load_channel_ids", return_value=[999]), \
         patch("domain.signals.robinhood_discovery.send_pump_card", new=AsyncMock()) as mock_send:
        stats = await run_robinhood_discovery_cycle(bot)

    assert stats["signals_sent"] == 0
    assert stats["tokens_rejected"] == 1
    assert not any(isinstance(o, RobinhoodDiscoverySignal) for o in session.added)
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_low_score_candidate_is_rejected_before_validation():
    weak_data = dict(HEALTHY_DATA, liquidity=50000, volume_1h=500, txns_1h_buys=2, txns_1h_sells=2, price_change_1h=-40)
    candidate_entry = {"contract": "RH3", "source": "dexscreener_new", "prefetched_data": weak_data}
    session = _FakeSession(existing_signal=None)
    bot = AsyncMock()

    with patch("domain.signals.robinhood_discovery.discover_candidates", new=AsyncMock(return_value=[candidate_entry])), \
         patch("domain.signals.robinhood_discovery.async_session", return_value=session), \
         patch("domain.signals.robinhood_discovery.build_validated_candidate", new=AsyncMock()) as mock_validate, \
         patch("domain.signals.robinhood_discovery.send_pump_card", new=AsyncMock()):
        stats = await run_robinhood_discovery_cycle(bot)

    assert stats["tokens_rejected"] >= 1
    assert stats["signals_sent"] == 0
    mock_validate.assert_not_awaited()  # never reaches the expensive safety pipeline


@pytest.mark.asyncio
async def test_cooldown_skips_already_alerted_robinhood_token():
    candidate_entry = {"contract": "RH4", "source": "dexscreener_new", "prefetched_data": HEALTHY_DATA}
    existing_signal = SimpleNamespace(cooldown_expires_at=_now() + timedelta(hours=5))
    session = _FakeSession(existing_signal=existing_signal)
    bot = AsyncMock()

    with patch("domain.signals.robinhood_discovery.discover_candidates", new=AsyncMock(return_value=[candidate_entry])), \
         patch("domain.signals.robinhood_discovery.async_session", return_value=session), \
         patch("domain.signals.robinhood_discovery.send_pump_card", new=AsyncMock()) as mock_send:
        stats = await run_robinhood_discovery_cycle(bot)

    assert stats["cooldown_skipped"] == 1
    assert stats["signals_sent"] == 0
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_failure_during_discovery_is_handled_gracefully():
    """15. Provider failure (DexScreener down) must not crash the cycle."""
    bot = AsyncMock()
    with patch("domain.signals.robinhood_discovery.discover_candidates", new=AsyncMock(side_effect=RuntimeError("DexScreener down"))):
        stats = await run_robinhood_discovery_cycle(bot)  # must not raise

    assert stats["pairs_scanned"] == 0
    assert stats["signals_sent"] == 0
