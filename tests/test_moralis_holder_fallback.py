from domain.intelligence._moralis_holder_fallback import _extract_accounts


def test_moralis_extracts_positive_balances_from_result_list():
    body = {
        "result": [
            {"ownerAddress": "wallet-a", "balance": "100.0"},
            {"ownerAddress": "wallet-b", "balance": "50.0"},
            {"ownerAddress": "wallet-zero", "balance": "0"},
        ]
    }
    accounts = _extract_accounts(body)
    assert accounts == [
        {"owner": "wallet-a", "amount": "100.0"},
        {"owner": "wallet-b", "amount": "50.0"},
    ]


def test_moralis_accepts_bare_array_response():
    body = [{"owner_address": "wallet-a", "amount": "42"}]
    accounts = _extract_accounts(body)
    assert accounts == [{"owner": "wallet-a", "amount": "42"}]


def test_moralis_rejects_malformed_payloads():
    assert _extract_accounts({"result": "bad"}) == []
    assert _extract_accounts(None) == []
    assert _extract_accounts({"error": "Token not found"}) == []
