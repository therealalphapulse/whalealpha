import json
import logging

from sqlalchemy import select

from infra.db.session import async_session
from models.signal_token import SignalToken

logger = logging.getLogger("AlphaPulse.SignalCalibration")

BREAKDOWN_COMPONENTS = (
    "liquidity_lp_integrity",
    "holder_distribution",
    "momentum_quality",
    "wallet_deployer_behavior",
    "narrative_social_multiplier",
    # Signal Engine re-evaluation: these two were already stored in every
    # SignalToken.entry_breakdown_json snapshot (see scoring.score_candidate's
    # `breakdown` dict) but were never included here, so the calibration
    # report had no way of telling you whether the smart-money/whale bonus
    # or the graduation heuristic actually predict outcomes any better than
    # the four original categories. Precision/false-positive tracking is a
    # named deliverable of this re-evaluation — this closes that gap using
    # the same real-outcome data (ath_multiple) already being collected,
    # not a new data source.
    "smart_money_whale_bonus",
    "graduation_bonus",
)

MIN_SAMPLE_SIZE = 15  # below this, correlations are noise, not signal


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    denom = (var_x * var_y) ** 0.5
    if denom == 0:
        return None
    return cov / denom


async def analyze_score_calibration() -> dict:
    """
    Uses your OWN signal history — not a new data source — to answer the
    question nothing in the pipeline currently checks: which parts of the
    conviction scorer actually predicted a winner vs. a rug, and which
    ones are dead weight?

    Pulls every SignalToken row that has both a stored entry_breakdown_json
    (see services/signal_tracker.create_signal_from_candidate) and a
    current ath_multiple, and computes the Pearson correlation between
    each of the 4 score components + the narrative multiplier and the
    realized ath_multiple.

    Returns:
        {
          "sample_size": int,
          "correlations": {component: float | None, ...},
          "note": str  # present when sample_size < MIN_SAMPLE_SIZE
        }

    A positive correlation means higher component score tended to predict
    a bigger eventual gain; near-zero or negative means that component
    isn't actually pulling its weight in the scorer and is a candidate for
    reweighting. This does NOT auto-adjust anything — it's a report for a
    human to act on, since blindly auto-tuning scoring weights off a
    modest sample would be its own source of overfitting.

    Only signals created AFTER this column was added will have
    entry_breakdown_json populated — historical signals from before this
    change are skipped, not treated as zero.
    """
    component_series: dict[str, list[float]] = {c: [] for c in BREAKDOWN_COMPONENTS}
    outcome_series: dict[str, list[float]] = {c: [] for c in BREAKDOWN_COMPONENTS}
    sample_size = 0

    async with async_session() as session:
        result = await session.execute(
            select(SignalToken.entry_breakdown_json, SignalToken.ath_multiple)
            .where(SignalToken.entry_breakdown_json.isnot(None))
            .where(SignalToken.ath_multiple.isnot(None))
        )
        rows = result.all()

    for breakdown_json, ath_multiple in rows:
        try:
            breakdown = json.loads(breakdown_json) if breakdown_json else {}
        except (json.JSONDecodeError, TypeError):
            continue
        if not breakdown or ath_multiple is None:
            continue

        sample_size += 1
        for component in BREAKDOWN_COMPONENTS:
            val = breakdown.get(component)
            if val is None:
                continue
            component_series[component].append(float(val))
            outcome_series[component].append(float(ath_multiple))

    correlations = {}
    for component in BREAKDOWN_COMPONENTS:
        xs = component_series[component]
        ys = outcome_series[component]
        correlations[component] = _pearson(xs, ys) if len(xs) >= 2 else None

    report = {
        "sample_size": sample_size,
        "correlations": correlations,
    }
    if sample_size < MIN_SAMPLE_SIZE:
        report["note"] = (
            f"Only {sample_size} signal(s) with stored breakdown data so far "
            f"(want {MIN_SAMPLE_SIZE}+ for the correlations to mean anything) — "
            "keep sending signals and re-run this later."
        )

    return report


def format_calibration_report(report: dict) -> str:
    """Plain-text rendering of analyze_score_calibration()'s output —
    for a Telegram admin command or a log line, caller's choice."""
    lines = [f"📐 Score Calibration Report — {report['sample_size']} signal(s) analyzed"]
    if report.get("note"):
        lines.append(f"⚠️ {report['note']}")
    lines.append("")
    for component, corr in report["correlations"].items():
        label = component.replace("_", " ").title()
        if corr is None:
            lines.append(f"• {label}: not enough data yet")
        else:
            direction = "predictive of gains" if corr > 0.1 else ("predictive of underperformance" if corr < -0.1 else "roughly no correlation")
            lines.append(f"• {label}: r={corr:+.2f} ({direction})")
    return "\n".join(lines)
