"""Pure functions turning raw on-chain swap history into a
`scoring.WalletMetrics`, so `engines/scoring.score_wallet` — the existing,
"do not modify" scoring algorithm — can be reused unchanged for candidates
the discovery engine finds, exactly as it already is for admin-added
wallets. No I/O here; fully unit-testable (tests/unit/test_discovery_metrics.py),
same testability shape as engines/scoring.py and engines/signal.py.

A wallet's realized PnL/ROI is computed FIFO: each SELL is matched against
the oldest still-open BUY lots for that mint, at that BUY's cost basis. This
is the standard, defensible convention for lot accounting absent an explicit
cost-basis method choice — flagged as a judgment call, same spirit as the
repo's other new-vs-ported pieces (see PORTING_NOTES.md).
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from whale_alpha.engines.scoring import WalletMetrics
from whale_alpha.integrations.wallet_discovery_source import WalletSwap


@dataclass(frozen=True)
class ComputedMetrics:
    """WalletMetrics plus the extra fields the discovery engine's own
    threshold gates need but scoring.WalletMetrics doesn't carry (it only
    needs what the scoring formula consumes)."""

    metrics: WalletMetrics
    trade_count_30d: int
    realized_pnl_usd_30d: float
    last_activity_at: datetime | None


def compute_wallet_metrics(
    swaps: list[WalletSwap],
    *,
    wallet_age_days: int | None,
    now: datetime | None = None,
) -> ComputedMetrics | None:
    """Returns None if there isn't enough swap history to compute a
    meaningful metrics set (fewer than 2 trades, or no closed round-trips at
    all) — callers should treat that as "not enough data yet", not "score 0".
    """
    now = now or datetime.now(UTC)
    if len(swaps) < 2:
        return None

    window_30d = now - timedelta(days=30)
    window_7d = now - timedelta(days=7)

    swaps_30d = [s for s in swaps if s.timestamp >= window_30d]
    swaps_7d = [s for s in swaps if s.timestamp >= window_7d]

    trade_count_30d = len(swaps_30d)
    trade_frequency_7d = float(len(swaps_7d))

    realized = _match_round_trips(swaps)
    if not realized:
        return None

    realized_30d = [r for r in realized if r.sell_time >= window_30d]
    pnl_usd_30d = sum(r.pnl_usd for r in realized_30d) if realized_30d else 0.0
    cost_basis_30d = sum(r.cost_basis_usd for r in realized_30d) if realized_30d else 0.0
    roi_30d = (pnl_usd_30d / cost_basis_30d) if cost_basis_30d > 0 else 0.0

    wins = sum(1 for r in realized if r.pnl_usd > 0)
    win_rate = wins / len(realized)
    trade_success_rate = win_rate  # same definition here: a "successful" trade is a profitable round-trip

    avg_hold_minutes = sum(r.hold_minutes for r in realized) / len(realized)

    buy_amounts = [s.amount_usd for s in swaps if s.side == "BUY" and s.amount_usd > 0]
    avg_position_usd = (sum(buy_amounts) / len(buy_amounts)) if buy_amounts else 0.0

    max_drawdown = _max_drawdown(realized)

    last_activity_at = max((s.timestamp for s in swaps), default=None)

    metrics = WalletMetrics(
        roi_30d=roi_30d,
        win_rate=win_rate,
        pnl_usd_30d=pnl_usd_30d,
        avg_hold_minutes=avg_hold_minutes,
        avg_position_usd=avg_position_usd,
        trade_frequency_7d=trade_frequency_7d,
        wallet_age_days=wallet_age_days if wallet_age_days is not None else 0,
        max_drawdown=max_drawdown,
        trade_success_rate=trade_success_rate,
    )

    return ComputedMetrics(
        metrics=metrics,
        trade_count_30d=trade_count_30d,
        realized_pnl_usd_30d=pnl_usd_30d,
        last_activity_at=last_activity_at,
    )


@dataclass(frozen=True)
class _RoundTrip:
    token_mint: str
    cost_basis_usd: float
    proceeds_usd: float
    pnl_usd: float
    hold_minutes: float
    sell_time: datetime


def _match_round_trips(swaps: list[WalletSwap]) -> list[_RoundTrip]:
    """FIFO-matches SELLs against open BUY lots, per token mint. Swaps must
    be provided in any order; this sorts by timestamp internally.

    Each SELL closes exactly one open BUY lot *in full* (one-to-one, oldest
    lot first) — not a proportional/partial match. This is a deliberate
    simplification: swap records here only carry a USD size (see
    integrations/wallet_discovery_source.py — we don't have on-chain token
    quantities, only the native-SOL side of each swap), so a BUY's dollar
    amount and a later SELL's dollar amount are NOT the same unit — the
    token may have appreciated or dropped in between. Treating a SELL as
    "the same $X of tokens sold for $Y" (full lot close) is correct;
    treating "$Y proceeds partially closes $X of cost basis" (comparing the
    two dollar figures directly, as if $1 of cost basis equals $1 of
    proceeds) is not, and produces wrong PnL. A SELL with no open lot to
    match (e.g. a position opened before our observation window started) is
    skipped — we have no cost basis for it, so it can't be scored.

    BUYs left unmatched at the end (still-open positions) are not counted —
    realized PnL only, consistent with WalletMetrics.pnl_usd_30d being a
    *realized* figure (not mark-to-market, which would need live per-token
    pricing this repo's schema doesn't track for the discovery engine — see
    auto_trading.py's similarly-documented approximation).
    """
    ordered = sorted(swaps, key=lambda s: s.timestamp)
    open_lots: dict[str, deque[WalletSwap]] = defaultdict(deque)
    round_trips: list[_RoundTrip] = []

    for swap in ordered:
        if swap.amount_usd <= 0:
            continue
        lots = open_lots[swap.token_mint]
        if swap.side == "BUY":
            lots.append(swap)
        elif swap.side == "SELL":
            if not lots:
                continue  # no open lot to match against — can't compute a cost basis, skip
            lot = lots.popleft()
            pnl = swap.amount_usd - lot.amount_usd
            hold_minutes = max(0.0, (swap.timestamp - lot.timestamp).total_seconds() / 60.0)
            round_trips.append(
                _RoundTrip(
                    token_mint=swap.token_mint,
                    cost_basis_usd=lot.amount_usd,
                    proceeds_usd=swap.amount_usd,
                    pnl_usd=pnl,
                    hold_minutes=hold_minutes,
                    sell_time=swap.timestamp,
                )
            )

    return round_trips


def _max_drawdown(round_trips: list[_RoundTrip]) -> float:
    """Peak-to-trough drawdown of the cumulative realized-PnL curve, as a
    fraction of the running peak equity (0..1). Returns 0 if equity never
    rose above its starting point (nothing to draw down from).
    """
    if not round_trips:
        return 0.0

    ordered = sorted(round_trips, key=lambda r: r.sell_time)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in ordered:
        equity += r.pnl_usd
        peak = max(peak, equity)
        if peak > 0:
            drawdown = (peak - equity) / peak
            max_dd = max(max_dd, drawdown)
    return min(1.0, max(0.0, max_dd))
