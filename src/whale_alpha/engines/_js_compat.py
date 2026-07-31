"""Tiny compatibility shims so ported numeric code matches JS semantics exactly.

Python's built-in `round()` uses banker's rounding (round-half-to-even), while
JavaScript's `Math.round()` always rounds half-way values up (toward positive
infinity). The original TS engines (`scoring`, `signal`) call `Math.round` on
values that are essentially always >= 0 (0..100 scores), where the two only
disagree on exact `.5` ties — rare in practice given the sigmoid-derived inputs,
but real. Per porting requirement #1 ("no behavior change to risk/signal/scoring
logic"), we replicate `Math.round` exactly rather than accept Python's rounding.
"""

from __future__ import annotations

import math


def js_round(x: float) -> int:
    """Replicates JavaScript's Math.round: round half away from... up (toward +inf)."""
    return math.floor(x + 0.5)
