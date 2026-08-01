"""Tests for the private-key parsing helper in bot/commands/wallet.py."""

from __future__ import annotations

import json

from solders.keypair import Keypair

from whale_alpha.bot.commands.wallet import _parse_secret_key


def test_parses_valid_base58_secret_key():
    keypair = Keypair()
    base58_secret = str(keypair)  # solders Keypair.__str__ returns base58 secret key
    parsed = _parse_secret_key(base58_secret)
    assert parsed is not None
    assert Keypair.from_bytes(parsed).pubkey() == keypair.pubkey()


def test_parses_valid_json_array_secret_key():
    keypair = Keypair()
    array_str = json.dumps(list(bytes(keypair)))
    parsed = _parse_secret_key(array_str)
    assert parsed is not None
    assert Keypair.from_bytes(parsed).pubkey() == keypair.pubkey()


def test_rejects_garbage_text():
    assert _parse_secret_key("not a key") is None


def test_rejects_empty_string():
    assert _parse_secret_key("") is None
    assert _parse_secret_key("   ") is None


def test_rejects_json_array_of_wrong_length():
    assert _parse_secret_key(json.dumps([1, 2, 3])) is None


def test_rejects_malformed_json_array():
    assert _parse_secret_key("[1, 2, not-json") is None
