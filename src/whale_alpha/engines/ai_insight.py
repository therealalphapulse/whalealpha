"""AI-written explanations for signals — purely cosmetic layer on top of the
deterministic scoring/signal engines (engines/scoring.py, engines/signal.py).

IMPORTANT: this module never influences confidence_score, risk_level, or
whether a signal fires at all. It only rewrites the human-facing
`ai_recommendation` string that gets sent to Telegram, replacing the old
templated if/elif text in signal.py with an explanation grounded in that
specific signal's actual numbers.

Fails safe by design: any error (missing key, timeout, malformed model
output, rate limit) falls back to the candidate's existing templated
ai_recommendation. A bad API call must never block a signal from firing or
being sent — see call site in engines/scheduler.py.
"""

from __future__ import annotations

import json

import anthropic

from whale_alpha.config import Env
from whale_alpha.engines.signal import SignalCandidate
from whale_alpha.utils.logger import child_logger

log = child_logger("ai_insight")

SYSTEM_PROMPT = """You are a risk-analysis assistant for a Solana whale-tracking \
signal bot. You receive a deterministically-scored signal candidate and its \
underlying wallet/safety data. Write a concise (2-3 sentence) trader-facing \
explanation of WHY this signal looks the way it does, referencing the \
specific numbers you're given.

Rules:
- Never invent data not present in the input.
- Never promise, imply, or predict returns.
- Never say "buy", "sell", or give a directive — describe, don't advise.
- If risk_flags is non-empty, the standout_risk field must name the single \
most material one in plain language.

Output ONLY valid JSON matching this schema, nothing else:
{"narrative": string, "standout_risk": string | null}"""

_client: anthropic.AsyncAnthropic | None = None


def _get_client(env: Env) -> anthropic.AsyncAnthropic | None:
    global _client
    if not env.ANTHROPIC_API_KEY:
        return None
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=env.ANTHROPIC_API_KEY)
    return _client


async def enrich_signal(
    env: Env,
    candidate: SignalCandidate,
    risk_flags: list[str] | None = None,
) -> str:
    """Returns a trader-facing explanation string for this signal.

    Falls back to candidate.ai_recommendation (the deterministic template)
    on any failure, missing key, or if AI_INSIGHTS_ENABLED is False —
    callers can treat this as always-succeeds.
    """
    fallback = candidate.ai_recommendation

    if not env.AI_INSIGHTS_ENABLED:
        return fallback

    client = _get_client(env)
    if client is None:
        return fallback

    payload = {
        "token_mint": candidate.token_mint,
        "wallet_count": candidate.wallet_count,
        "total_capital_usd": round(candidate.total_capital_usd, 2),
        "confidence_score": candidate.confidence_score,
        "risk_level": candidate.risk_level,
        "risk_flags": risk_flags or [],
    }

    try:
        resp = await client.messages.create(
            model=env.AI_INSIGHTS_MODEL,
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload)}],
            timeout=env.AI_INSIGHTS_TIMEOUT_SECONDS,
        )
    except anthropic.APIError as err:
        log.warning("AI insight call failed, using templated fallback", err=str(err))
        return fallback

    text = "".join(block.text for block in resp.content if block.type == "text")

    try:
        parsed = json.loads(text)
        narrative = parsed["narrative"]
        if not isinstance(narrative, str):
            raise TypeError("narrative field was not a string")
    except (json.JSONDecodeError, KeyError, TypeError) as err:
        log.warning("AI insight returned unparseable output, using templated fallback", err=str(err))
        return fallback

    standout_risk = parsed.get("standout_risk")
    if standout_risk:
        return f"{narrative} Standout risk: {standout_risk}."
    return narrative
