from domain.intelligence._solana_tracker_holder_fallback import _extract_accounts


def test_tracker_extracts_positive_wallet_balances_and_total():
    body = {
        "total": 3,
        "accounts": [
            {"wallet": "wallet-a", "amount": 100.0, "percentage": 50.0},
            {"wallet": "wallet-b", "amount": 50.0, "percentage": 25.0},
            {"wallet": "wallet-zero", "amount": 0.0, "percentage": 0.0},
        ],
        "cursor": "next",
        "hasMore": True,
    }
    accounts, total, has_more = _extract_accounts(body)
    assert accounts == [
        {"owner": "wallet-a", "amount": "100.0"},
        {"owner": "wallet-b", "amount": "50.0"},
    ]
    assert total == 3
    assert has_more is True


def test_tracker_rejects_error_or_malformed_payloads():
    assert _extract_accounts({"error": "Token not found"}) == ([], None, False)
    assert _extract_accounts({"accounts": "bad"}) == ([], None, False)


def test_tracker_accepts_owner_alias_for_normalization():
    accounts, total, has_more = _extract_accounts(
        {"total": 1, "accounts": [{"owner": "wallet-a", "amount": "42"}]}
    )
    assert accounts == [{"owner": "wallet-a", "amount": "42"}]
    assert total == 1
    assert has_more is False
