from whale_alpha.bot.commands.scanner import is_plain_contract_address


VALID_SOLANA_ADDRESS = "So11111111111111111111111111111111111111112"


def test_plain_contract_address_accepts_bare_solana_address():
    assert is_plain_contract_address(VALID_SOLANA_ADDRESS) is True
    assert is_plain_contract_address(f"  {VALID_SOLANA_ADDRESS}  ") is True


def test_plain_contract_address_rejects_commands_and_non_addresses():
    assert is_plain_contract_address(f"/scan {VALID_SOLANA_ADDRESS}") is False
    assert is_plain_contract_address("hello whale alpha") is False
    assert is_plain_contract_address("") is False
    assert is_plain_contract_address(None) is False
