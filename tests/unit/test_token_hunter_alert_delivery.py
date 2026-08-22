from whale_alpha.engines.token_hunter import alert_recipient_ids


def test_admin_recipients_take_priority():
    assert alert_recipient_ids({"100"}, ["200", "300"]) == ["100"]


def test_subscribed_users_are_fallback_when_no_admins():
    assert alert_recipient_ids(set(), ["200", "300"]) == ["200", "300"]


def test_duplicate_subscriber_ids_are_removed():
    assert alert_recipient_ids(set(), ["200", "200", "300"]) == ["200", "300"]


def test_no_recipients_returns_empty_list():
    assert alert_recipient_ids(set(), []) == []
