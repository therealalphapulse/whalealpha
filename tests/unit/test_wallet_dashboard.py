from whale_alpha.bot.commands.wallet import _format_amount, _short_address, _wallet_card


def test_short_address():
    assert _short_address("1234567890ABCDEFGHIJ") == "123456...GHIJ"


def test_format_amount():
    assert _format_amount(1234.5) == "1,234.5"
    assert _format_amount(0.0001234) == "0.0001234"


def test_wallet_card_contains_live_wallet_summary():
    card = _wallet_card("1234567890ABCDEFGHIJ", 2.5, None, [("MintAddress123456789", 1000.0)])
    assert "YOUR WALLET" in card
    assert "2.5 SOL" in card
    assert "token holdings" in card
    assert "Mint...6789" in card
