from datetime import UTC, datetime
from types import SimpleNamespace

from whale_alpha.engines.token_hunter import TokenScore, format_alert
from whale_alpha.engines.token_hunter import quote_milestones_for_gain
from whale_alpha.services.notification import format_signal_message


def test_token_hunter_alert_card_has_professional_sections():
    snapshot = SimpleNamespace(
        symbol="ALPHA", name="Alpha Token", mint="Mint123456789", market_cap_usd=125_000,
        liquidity_usd=42_000, volume_5m_usd=18_500, buys_5m=42, sells_5m=11, price_change_1h_pct=27.4,
    )
    score = TokenScore(86, {"age": 92}, "LOW", (), ("Organic activity", "Liquidity health"))
    card = format_alert(snapshot, score, 18, datetime(2026, 8, 22, 17, 0, tzinfo=UTC))
    for marker in ("WHALE ALPHA", "MARKET SNAPSHOT", "WHY IT TRIGGERED", "RISK CHECK", "CONTRACT", "86/100"):
        assert marker in card
    assert "<b>" in card and "<code>" in card


def test_signal_card_has_confidence_whales_entry_and_action():
    signal = SimpleNamespace(entry_zone_low=0.001, entry_zone_high=0.002)
    candidate = SimpleNamespace(
        token_mint="Mint123456789", confidence_score=91, risk_level="LOW", wallet_count=7,
        total_capital_usd=12500, ai_recommendation="Strong accumulation; monitor liquidity.",
    )
    card = format_signal_message(signal, candidate)
    for marker in ("WHALE ALPHA SIGNAL", "Confidence", "WHALE ACTIVITY", "ENTRY ZONE", "ALPHA READ", "/buy", "/autotrading"):
        assert marker in card
    assert "Strong accumulation" in card




def test_quote_milestones_progress_from_percent_to_multiples():
    assert quote_milestones_for_gain(24.9) == []
    assert quote_milestones_for_gain(80) == [25, 50, 75]
    assert quote_milestones_for_gain(100) == [25, 50, 75, 100]
    assert quote_milestones_for_gain(199.9) == [25, 50, 75, 100]
    assert quote_milestones_for_gain(315) == [25, 50, 75, 100, 200, 300]
    assert quote_milestones_for_gain(815) == [25, 50, 75, 100, 200, 300, 400, 500, 600, 700, 800]
