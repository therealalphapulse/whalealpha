import asyncio
from unittest.mock import AsyncMock, patch
from domain.trading.real import robinhood_wallet

def test_evm_private_key_import_is_hex():
    raw="0x"+"11"*32
    parsed=robinhood_wallet._parse_private_key_input(raw)
    assert parsed==bytes.fromhex("11"*32)

def test_invalid_private_key_rejected():
    try: robinhood_wallet._parse_private_key_input("not-a-key")
    except robinhood_wallet.WalletImportError: return
    raise AssertionError("invalid key was accepted")
