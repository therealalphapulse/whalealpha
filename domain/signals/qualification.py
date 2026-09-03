"""Pure qualification decision layer.

Qualification is independent from quota state. Quota may choose the active
cutoff, but it must never mutate, replace, or inflate the candidate's quality
score. The scorer owns legitimate score adjustments; this module decides
whether the candidate's effective final score clears the active cutoff.

``raw_score`` is retained as the backward-compatible base score. When the
scorer supplies ``final_score``, that value is the authoritative quality
score for qualification because it contains the legitimate, already-bounded
score adjustments produced by the scorer. Quota state is never allowed to
create or mutate that final score.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class QualificationDecision:
    qualified: bool
    reason: str


def qualify_candidate(
    raw_score: float,
    dynamic_cutoff: float,
    final_score: float | None = None,
) -> QualificationDecision:
    """Qualify from the candidate's effective final score.

    ``raw_score`` remains the fallback for legacy callers that do not provide
    ``final_score``. When ``final_score`` is present, it is the authoritative
    score because it represents the scorer's legitimate post-adjustment
    quality value. This keeps score calculation and qualification separate:
    this function never changes either score and never consults quota state.
    """
    effective_score = raw_score if final_score is None else final_score
    if effective_score < dynamic_cutoff:
        return QualificationDecision(False, "BELOW_DYNAMIC_CUTOFF")
    return QualificationDecision(True, "SCORE_ABOVE_CUTOFF")


# ---------------------------------------------------------------------
# Signal Quality & Alert Qualification Upgrade
#
# Before this, a candidate's send decision ran through TWO fully
# independent hard gates, evaluated serially, each able to unilaterally
# kill an otherwise-legitimate candidate on its own:
#   1. qualify_candidate() above (final_score >= dynamic_cutoff)
#   2. scoring.compute_confidence()["meets_bar"] (confidence_score >=
#      MIN_CONFIDENCE_SCORE AND confirmed_count >= required_confirmations),
#      applied afterward in pump_radar.analyze_candidate() as a second,
#      unrelated veto.
#
# That made the SCORE a single point of failure (a token one point below
# dynamic_cutoff was discarded no matter how strong and well-corroborated
# its evidence was) and, independently, made CONFIDENCE a second single
# point of failure (a token that cleared the score cutoff with clean risk
# characteristics could still be thrown away for evidence breadth alone,
# e.g. a brand-new token where most of the expensive verification lookups
# legitimately haven't resolved yet).
#
# evaluate_signal_readiness() below replaces that two-gate stack with one
# combined decision. It does not lower either bar and does not touch any
# hard risk gate (hard_reject_reasons / evaluate_verified_red_flags /
# passes_mc_liquidity_gate — all evaluated earlier, unchanged, and still
# an absolute veto regardless of score or confidence). What it fixes is
# narrower and more specific: a candidate that is genuinely strong on ONE
# of {score, confidence} and only marginally short on the OTHER is no
# longer discarded by that single marginal shortfall — the combination of
# evidence decides, per the "signal readiness" principle (Risk Integrity +
# Market Structure + Momentum + Flow/Buy Pressure + Holder Quality +
# Available Supporting Evidence + Score), rather than either dimension
# alone.
#
# qualify_candidate() above is left completely untouched (existing
# callers/tests keep working unchanged) — evaluate_signal_readiness() is
# additive, not a replacement of that function's public contract.
# ---------------------------------------------------------------------

# Small, principled "near-miss forgiveness" windows — NOT a lowering of
# either bar. A candidate short by more than this on a dimension is still
# rejected outright on that dimension; these only define how close
# "close enough to potentially be compensated for" is.
SCORE_NEAR_MISS_BAND = 4.0        # final_score points below dynamic_cutoff
CONFIDENCE_NEAR_MISS_BAND = 12.0  # confidence_score points below MIN_CONFIDENCE_SCORE

# How far ABOVE its own bar the *other* dimension must sit to forgive a
# near-miss on this one. Deliberately larger than the near-miss bands
# themselves, so compensation only ever fires when the other dimension is
# genuinely strong, not merely "also passing" — a bounded trade-off, not
# a loophole.
COMPENSATION_STRENGTH_MARGIN = 8.0

# Signal-readiness tiers (Elite / Strong / Emerging-Watch / Reject) — the
# send-decision tier. Distinct from, and complementary to,
# scoring.TIER_LABELS: TIER_LABELS buckets raw final_score alone for
# observability/logging; these tiers describe the actual combined
# readiness decision made here, including the compensation path.
READY_ELITE = "ELITE"
READY_STRONG = "STRONG"
READY_EMERGING_WATCH = "EMERGING_WATCH"
READY_REJECT = "REJECT"

# How far above both bars simultaneously counts as "Elite" rather than
# just "Strong" — a clean pass on both dimensions with real headroom on
# each, not merely clearing them.
ELITE_MARGIN = 15.0


@dataclass(frozen=True)
class SignalReadinessDecision:
    ready: bool
    tier: str  # READY_ELITE | READY_STRONG | READY_EMERGING_WATCH | READY_REJECT
    reason: str
    score_gap: float        # dynamic_cutoff - final_score (<=0 means cleared)
    confidence_gap: float   # min_confidence_score - confidence_score (<=0 means cleared)


def candidate_worth_full_enrichment(
    final_score: float,
    dynamic_cutoff: float,
) -> bool:
    """Cheap pre-filter, evaluated BEFORE the expensive per-candidate
    lookups that feed confidence (LP lock, funding graph, deployer
    history, price cross-check) — preserves the existing resource-
    protection intent (don't spend paid/rate-limited calls on hopeless
    candidates) while widening the window just enough that a candidate
    within SCORE_NEAR_MISS_BAND of the cutoff still gets a chance to
    reach evaluate_signal_readiness() below, where strong, verified
    evidence can legitimately compensate for a small score shortfall.
    This is NOT the final accept/reject decision — a candidate that
    passes this may still be rejected by evaluate_signal_readiness()
    once real confidence data is available.
    """
    return final_score >= (dynamic_cutoff - SCORE_NEAR_MISS_BAND)


def evaluate_signal_readiness(
    final_score: float,
    dynamic_cutoff: float,
    confidence: dict,
    min_confidence_score: float,
) -> SignalReadinessDecision:
    """Combined score + confidence/evidence readiness decision.

    ``confidence`` is the dict returned by scoring.compute_confidence():
    {"confidence_score", "confirmed_count", "checked_count",
    "required_confirmations", "meets_bar"}.

    ``confirmed_count >= required_confirmations`` (the "multiple
    independent things must actually agree" floor) is enforced
    unconditionally here, exactly as strict as before — that is the
    genuine "avoid random tokens" requirement and is not something this
    function loosens. What changes is that ``final_score >=
    dynamic_cutoff`` and ``confidence_score >= min_confidence_score`` are
    no longer independent vetoes: each can forgive a small (near-miss)
    shortfall in the other when it is itself comfortably clear of its own
    bar (by at least COMPENSATION_STRENGTH_MARGIN). A candidate that is
    weak on both, or short by more than the near-miss band on the one
    dimension it's compensating for, is still rejected.
    """
    confidence_score = confidence.get("confidence_score", 0.0)
    confirmed_count = confidence.get("confirmed_count", 0)
    checked_count = confidence.get("checked_count", 0)
    required_confirmations = confidence.get("required_confirmations", 0)

    score_gap = dynamic_cutoff - final_score
    confidence_gap = min_confidence_score - confidence_score

    confirmations_ok = checked_count > 0 and confirmed_count >= required_confirmations

    if not confirmations_ok:
        return SignalReadinessDecision(
            False, READY_REJECT,
            f"INSUFFICIENT_CONFIRMATIONS ({confirmed_count}/{required_confirmations} required, "
            f"{checked_count} checks resolved)",
            score_gap, confidence_gap,
        )

    score_ok = score_gap <= 0
    confidence_ok = confidence_gap <= 0

    if score_ok and confidence_ok:
        if score_gap <= -ELITE_MARGIN and confidence_gap <= -ELITE_MARGIN:
            return SignalReadinessDecision(True, READY_ELITE, "CLEARS_BOTH_WITH_MARGIN", score_gap, confidence_gap)
        return SignalReadinessDecision(True, READY_STRONG, "CLEARS_BOTH", score_gap, confidence_gap)

    # Compensation path: one dimension is a near-miss, the other is
    # comfortably strong (not just passing) — combined evidence still
    # supports signal-readiness, per the Signal Readiness principle that
    # score is one input among several, never the sole determinant.
    score_is_near_miss = 0 < score_gap <= SCORE_NEAR_MISS_BAND
    confidence_is_near_miss = 0 < confidence_gap <= CONFIDENCE_NEAR_MISS_BAND

    if score_is_near_miss and confidence_ok and (-confidence_gap) >= COMPENSATION_STRENGTH_MARGIN:
        return SignalReadinessDecision(
            True, READY_EMERGING_WATCH,
            f"SCORE_NEAR_MISS_COMPENSATED_BY_STRONG_EVIDENCE (score {score_gap:.1f} below cutoff, "
            f"confidence {-confidence_gap:.1f} above bar)",
            score_gap, confidence_gap,
        )

    if confidence_is_near_miss and score_ok and (-score_gap) >= COMPENSATION_STRENGTH_MARGIN:
        return SignalReadinessDecision(
            True, READY_EMERGING_WATCH,
            f"CONFIDENCE_NEAR_MISS_COMPENSATED_BY_STRONG_SCORE (confidence {confidence_gap:.1f} "
            f"below bar, score {-score_gap:.1f} above cutoff)",
            score_gap, confidence_gap,
        )

    if not score_ok and not confidence_ok:
        return SignalReadinessDecision(
            False, READY_REJECT,
            f"BELOW_BOTH_SCORE_AND_CONFIDENCE (score short by {score_gap:.1f}, "
            f"confidence short by {confidence_gap:.1f})",
            score_gap, confidence_gap,
        )
    if not score_ok:
        return SignalReadinessDecision(
            False, READY_REJECT,
            f"BELOW_DYNAMIC_CUTOFF (short by {score_gap:.1f}, beyond near-miss/compensation range)",
            score_gap, confidence_gap,
        )
    return SignalReadinessDecision(
        False, READY_REJECT,
        f"BELOW_CONFIDENCE_BAR (short by {confidence_gap:.1f}, beyond near-miss/compensation range)",
        score_gap, confidence_gap,
    )
