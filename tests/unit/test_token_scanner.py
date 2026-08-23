from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot
from whale_alpha.services.token_scanner import build_scan_card


def snapshot(**overrides):
    values = dict(
        mint="So11111111111111111111111111111111111111112X",
        name="Test Meme",
        symbol="MEME",
        pair_address="pair123",
        dex_id="raydium",
        created_at_ms=1_000_000,
        price_usd=0.00001234,
        market_cap_usd=125_000,
        liquidity_usd=32_000,
        volume_5m_usd=9_500,
        volume_1h_usd=80_000,
        buys_5m=42,
        sells_5m=11,
        buys_1h=210,
        sells_1h=80,
        price_change_5m_pct=12.5,
        price_change_1h_pct=48.2,
        metadata_present=True,
    )
    values.update(overrides)
    return TokenMarketSnapshot(**values)


def test_build_scan_card_contains_live_market_sections():
    card = build_scan_card(snapshot(), now_ms=1_000_000 + 30 * 60_000)
    assert "Whale Alpha" in card
    assert "$MEME" in card
    assert "⏱ Age:" in card
    assert "💰 MC:" in card
    assert "💧 Liq:" in card
    assert "📊 Vol:" in card
    assert "🛠 Dev:" in card
    assert "📦 Bundles:" in card
    assert "Alpha Read" in card
    assert "$125.00K" in card
    assert "$32.0K" in card
    assert "+12.50%" in card
    assert "42 buys / 11 sells" in card
    assert "<code>pair123</code>" in card


def test_scanner_does_not_hide_low_market_cap_tokens():
    card = build_scan_card(snapshot(market_cap_usd=2_500, liquidity_usd=3_000), now_ms=1_000_000)
    assert "$2.50K" in card
    assert "Very low liquidity" in card


def test_scanner_marks_sell_pressure():
    card = build_scan_card(snapshot(buys_5m=2, sells_5m=9))
    assert "Heavy 5m sell pressure" in card
