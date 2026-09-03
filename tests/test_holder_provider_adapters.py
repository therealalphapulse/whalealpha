from domain.intelligence.bitquery_holder_diagnostic import (
    _extract_v1_holder_accounts,
    _extract_v2_holder_accounts,
)


def test_bitquery_v2_extracts_positive_owner_balances():
    body = {
        "data": {
            "Solana": {
                "BalanceUpdates": [
                    {"BalanceUpdate": {"Account": {"Owner": "wallet-a"}, "Holding": "123.45"}},
                    {"BalanceUpdate": {"Account": {"Owner": "wallet-b"}, "Holding": "0"}},
                ]
            }
        }
    }
    assert _extract_v2_holder_accounts(body) == [{"owner": "wallet-a", "amount": "123.45"}]


def test_bitquery_v1_extracts_positive_net_balances():
    body = {
        "data": {
            "solana": {
                "transfers": [
                    {"receiver": {"address": "wallet-a"}, "balance": "42.0"},
                    {"receiver": {"address": "wallet-b"}, "balance": "0"},
                ]
            }
        }
    }
    assert _extract_v1_holder_accounts(body) == [{"owner": "wallet-a", "amount": "42.0"}]


def test_bitquery_extractors_reject_error_or_malformed_payloads():
    assert _extract_v2_holder_accounts({"errors": [{"message": "bad query"}]}) is None
    assert _extract_v1_holder_accounts({"data": {"solana": {"transfers": "bad"}}}) is None
