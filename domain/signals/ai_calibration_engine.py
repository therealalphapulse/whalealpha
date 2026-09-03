"""Shadow-mode AI self-learning calibration foundation for AlphaPulse.

This module is advisory only. It reads existing SignalToken history and
produces calibration statistics. It MUST NOT change signal filters, scoring,
gates, quotas, PumpRadar discovery, Quote Alerts, or RealWallet behavior.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from infra.db.session import async_session
from models.signal_token import SignalToken

logger = logging.getLogger("AlphaPulse.AICalibration")

TARGETS: tuple[tuple[float, str], ...] = (
    (1.25, "+25%"), (1.50, "+50%"), (2.00, "2X"),
    (3.00, "3X"), (4.00, "4X"), (5.00, "5X"), (10.00, "10X"),
)


@dataclass(frozen=True)
class CalibrationBucket:
    name: str
    sample_size: int
    mean_ath_multiple: float
    median_ath_multiple: float
    reach_rates: dict[str, float]


@dataclass(frozen=True)
class CalibrationReport:
    generated_at: str
    total_signals: int
    matured_signals: int
    immature_signals: int
    minimum_mature_age_hours: float
    buckets: list[CalibrationBucket]
    component_correlations: dict[str, float]
    recommendations: list[str]
    shadow_only: bool = True


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def _pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3 or len(xs) != len(ys):
        return 0.0
    xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
    numerator = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    denominator = (sum((x - xm) ** 2 for x in xs) * sum((y - ym) ** 2 for y in ys)) ** 0.5
    return numerator / denominator if denominator else 0.0


def _source_bucket(signal: SignalToken) -> str:
    """Best-effort source segmentation without changing the live schema."""
    raw = signal.entry_breakdown_json
    if not raw:
        return "unknown"
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return "unknown"
    source = str(data.get("discovery_source") or data.get("source") or "unknown").lower()
    if "react" in source or "older" in source:
        return "reactivation"
    if "fresh" in source or "pump" in source:
        return "fresh"
    return "unknown"


def _mature(signaled_at: datetime | None, now: datetime, min_age_hours: float) -> bool:
    if not signaled_at:
        return False
    if signaled_at.tzinfo is None:
        signaled_at = signaled_at.replace(tzinfo=timezone.utc)
    return (now - signaled_at).total_seconds() >= min_age_hours * 3600.0


def build_calibration_report(
    signals: list[SignalToken],
    *,
    now: datetime | None = None,
    minimum_mature_age_hours: float = 24.0,
) -> CalibrationReport:
    """Build a deterministic, shadow-only report from existing signal data."""
    now = now or datetime.now(timezone.utc)
    mature = [
        s for s in signals
        if _mature(s.signaled_at, now, minimum_mature_age_hours)
        and _safe_float(s.ath_multiple, 1.0) > 0
    ]

    grouped: dict[str, list[SignalToken]] = defaultdict(list)
    for signal in mature:
        grouped[_source_bucket(signal)].append(signal)

    buckets: list[CalibrationBucket] = []
    for name, rows in sorted(grouped.items()):
        multiples = [_safe_float(s.ath_multiple, 1.0) for s in rows]
        reach_rates = {
            label: sum(1 for m in multiples if m >= target) / len(multiples)
            for target, label in TARGETS
        }
        buckets.append(CalibrationBucket(
            name=name,
            sample_size=len(rows),
            mean_ath_multiple=sum(multiples) / len(multiples),
            median_ath_multiple=_median(multiples),
            reach_rates=reach_rates,
        ))

    component_values: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for signal in mature:
        try:
            breakdown = json.loads(signal.entry_breakdown_json or "{}")
            outcome = _safe_float(signal.ath_multiple, 1.0)
            for key, value in breakdown.items():
                if isinstance(value, (int, float)):
                    component_values[str(key)].append((float(value), outcome))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    correlations = {
        key: round(_pearson([x for x, _ in pairs], [y for _, y in pairs]), 4)
        for key, pairs in component_values.items()
        if len(pairs) >= 3
    }

    recommendations: list[str] = []
    if len(mature) < 30:
        recommendations.append(
            "Collect more mature signal outcomes before trusting calibration recommendations."
        )
    if len(mature) >= 30 and not correlations:
        recommendations.append(
            "Entry component snapshots are insufficient for component-level calibration yet."
        )
    if mature and buckets:
        best = max(buckets, key=lambda bucket: bucket.mean_ath_multiple)
        if best.name != "unknown":
            recommendations.append(
                f"Monitor {best.name} separately; its historical outcome distribution differs from other buckets."
            )

    return CalibrationReport(
        generated_at=now.isoformat(),
        total_signals=len(signals),
        matured_signals=len(mature),
        immature_signals=max(0, len(signals) - len(mature)),
        minimum_mature_age_hours=minimum_mature_age_hours,
        buckets=buckets,
        component_correlations=correlations,
        recommendations=recommendations,
        shadow_only=True,
    )


async def collect_calibration_report(*, minimum_mature_age_hours: float = 24.0) -> CalibrationReport:
    """Read existing signal history and return a shadow calibration report."""
    async with async_session() as session:
        result = await session.execute(select(SignalToken).order_by(SignalToken.signaled_at.asc()))
        signals = list(result.scalars().all())

    report = build_calibration_report(
        signals,
        minimum_mature_age_hours=minimum_mature_age_hours,
    )
    logger.info(
        "AI calibration shadow report: signals=%d mature=%d buckets=%d recommendations=%d",
        report.total_signals,
        report.matured_signals,
        len(report.buckets),
        len(report.recommendations),
    )
    return report


def report_to_dict(report: CalibrationReport) -> dict[str, Any]:
    return asdict(report)


__all__ = [
    "CalibrationBucket", "CalibrationReport", "TARGETS",
    "build_calibration_report", "collect_calibration_report", "report_to_dict",
]
