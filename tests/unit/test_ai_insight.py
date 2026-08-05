"""Tests for engines/ai_insight.py. The Anthropic client is monkeypatched so
no real network call or API key is needed — these only exercise the
enrich/fallback logic, not Claude's actual output quality.
"""

from __future__ import annotations

import json

import pytest

from whale_alpha.engines import ai_insight
from whale_alpha.engines.signal import EntryZone, SignalCandidate


class _Env:
    """Minimal stand-in for whale_alpha.config.Env — only the fields
    ai_insight.py actually reads."""

    ANTHROPIC_API_KEY: str | None = "sk-ant-test-key"
    AI_INSIGHTS_ENABLED: bool = True
    AI_INSIGHTS_MODEL: str = "claude-haiku-4-5"
    AI_INSIGHTS_TIMEOUT_SECONDS: float = 8.0


def make_candidate(**overrides) -> SignalCandidate:
    defaults = dict(
        token_mint="TOKEN_MINT_ABC",
        wallet_count=4,
        total_capital_usd=12000.0,
        confidence_score=78,
        risk_level="MEDIUM",
        entry_zone=EntryZone(low=1.0, high=1.1),
        ai_recommendation="Moderate consensus. Position within your normal risk limits and monitor closely.",
        contributing_wallets=["w1", "w2", "w3", "w4"],
    )
    defaults.update(overrides)
    return SignalCandidate(**defaults)


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    ai_insight._client = None
    yield
    ai_insight._client = None


async def test_returns_templated_fallback_when_no_api_key():
    env = _Env()
    env.ANTHROPIC_API_KEY = None
    candidate = make_candidate()

    result = await ai_insight.enrich_signal(env, candidate)

    assert result == candidate.ai_recommendation


async def test_returns_templated_fallback_when_disabled():
    env = _Env()
    env.AI_INSIGHTS_ENABLED = False
    candidate = make_candidate()

    result = await ai_insight.enrich_signal(env, candidate)

    assert result == candidate.ai_recommendation


async def test_uses_model_narrative_on_success(monkeypatch):
    env = _Env()
    candidate = make_candidate()
    payload = {"narrative": "Four wallets accumulated $12,000 within the window.", "standout_risk": None}

    class _FakeMessages:
        async def create(self, **kwargs):
            assert kwargs["model"] == env.AI_INSIGHTS_MODEL
            return _FakeResponse(json.dumps(payload))

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(ai_insight, "_get_client", lambda _env: _FakeClient())

    result = await ai_insight.enrich_signal(env, candidate)

    assert result == payload["narrative"]


async def test_appends_standout_risk_when_present(monkeypatch):
    env = _Env()
    candidate = make_candidate()
    payload = {"narrative": "Solid cluster.", "standout_risk": "liquidity is thin relative to position size"}

    class _FakeMessages:
        async def create(self, **kwargs):
            return _FakeResponse(json.dumps(payload))

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(ai_insight, "_get_client", lambda _env: _FakeClient())

    result = await ai_insight.enrich_signal(env, candidate)

    assert result == "Solid cluster. Standout risk: liquidity is thin relative to position size."


async def test_falls_back_on_malformed_json(monkeypatch):
    env = _Env()
    candidate = make_candidate()

    class _FakeMessages:
        async def create(self, **kwargs):
            return _FakeResponse("not valid json")

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(ai_insight, "_get_client", lambda _env: _FakeClient())

    result = await ai_insight.enrich_signal(env, candidate)

    assert result == candidate.ai_recommendation
