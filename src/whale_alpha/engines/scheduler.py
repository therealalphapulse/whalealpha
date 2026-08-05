# import block — added one line:
from whale_alpha.engines.ai_insight import enrich_signal

# ...inside _evaluate_all_tokens, right after the duplicate-signal check:
            if existing_result.scalar_one_or_none() is not None:
                continue  # avoid duplicate signals for the same cluster

            # Best-effort: rewrite the templated ai_recommendation with a
            # Claude-written explanation grounded in this candidate's actual
            # numbers. Never raises — falls back to the template on any
            # failure (see engines/ai_insight.py docstring).
            candidate.ai_recommendation = await enrich_signal(env, candidate)

            entry_zone_low = entry_zone_high = None
