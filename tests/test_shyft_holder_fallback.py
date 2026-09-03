from domain.intelligence._shyft_holder_fallback import _extract_accounts


def test_shyft_extracts_positive_balances_and_detects_full_page():
    body = {
        "success": True,
        "result": [{"owner": f"wallet-{i}", "amount": str(100 - i)} for i in range(100)],
    }
    accounts, has_more = _extract_accounts(body)
    assert len(accounts) == 100
    assert has_more is True


def test_shyft_extracts_owner_alias_fields():
    body = {"success": True, "result": [{"address": "wallet-a", "balance": "10"}]}
    accounts, has_more = _extract_accounts(body)
    assert accounts == [{"owner": "wallet-a", "amount": "10"}]
    assert has_more is False


def test_shyft_rejects_error_or_malformed_payloads():
    assert _extract_accounts({"success": False}) == ([], False)
    assert _extract_accounts({"success": True, "result": "bad"}) == ([], False)
    assert _extract_accounts(None) == ([], False)
