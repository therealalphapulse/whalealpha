"""Tests for the whale-buy extraction logic in integrations/helius_webhook.py.

Only exercises the pure `_extract_buy_events` parser (no network, no aiohttp
app) — see that module's docstring for the payload-shape assumption this is
built against.
"""

from __future__ import annotations

from whale_alpha.integrations.helius_webhook import _extract_buy_events
from whale_alpha.integrations.price_feed import SOL_MINT

WALLET = "6ZRCB7AAqGre6c72VxSXff9RKzXPT8t4nSJ4mBcTHnnT"
TOKEN_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _tx(**overrides):
    base = {
        "signature": "sig123",
        "tokenTransfers": [
            {"fromUserAccount": "someone_else", "toUserAccount": WALLET, "mint": TOKEN_MINT, "tokenAmount": 100.5}
        ],
    }
    base.update(overrides)
    return base


def test_extracts_a_single_buy_event():
    events = _extract_buy_events(_tx())
    assert len(events) == 1
    assert events[0]["wallet_address"] == WALLET
    assert events[0]["token_mint"] == TOKEN_MINT
    assert events[0]["amount_tokens"] == 100.5
    assert events[0]["tx_signature"] == "sig123"


def test_ignores_sol_transfers():
    events = _extract_buy_events(
        _tx(tokenTransfers=[{"toUserAccount": WALLET, "mint": SOL_MINT, "tokenAmount": 5.0}])
    )
    assert events == []


def test_ignores_transfers_with_no_signature():
    tx = _tx()
    del tx["signature"]
    assert _extract_buy_events(tx) == []


def test_ignores_zero_or_negative_amounts():
    events = _extract_buy_events(
        _tx(tokenTransfers=[{"toUserAccount": WALLET, "mint": TOKEN_MINT, "tokenAmount": 0}])
    )
    assert events == []


def test_ignores_malformed_transfer_entries():
    events = _extract_buy_events(
        _tx(tokenTransfers=[{"toUserAccount": WALLET, "mint": TOKEN_MINT, "tokenAmount": "not-a-number"}])
    )
    assert events == []


def test_handles_multiple_transfers_in_one_transaction():
    other_mint = "So11111111111111111111111111111111111111112x"
    events = _extract_buy_events(
        _tx(
            tokenTransfers=[
                {"toUserAccount": WALLET, "mint": TOKEN_MINT, "tokenAmount": 10},
                {"toUserAccount": WALLET, "mint": other_mint, "tokenAmount": 20},
            ]
        )
    )
    assert len(events) == 2
    assert {e["token_mint"] for e in events} == {TOKEN_MINT, other_mint}
