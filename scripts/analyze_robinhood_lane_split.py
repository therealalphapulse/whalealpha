"""
scripts/analyze_robinhood_lane_split.py

Read-only, one-off. Splits every Robinhood Chain signal by discovery
lane (FRESH vs REVIVAL, inferred from entry_breakdown_json -- REVIVAL
rows carry drawdown_pct/recovery_pct keys from
robinhood_discovery.score_revival_potential(), FRESH rows never do)
and by ath_multiple outcome, to answer two specific questions:

  1. Which lane produced the big (>=10x, >=30x) runners?
  2. Within each lane, what fraction of signals died before reaching
     even the first milestone (no genuine positive SignalEvent) vs
     went on to pay out?

Never mutates anything. Not wired into any startup/deploy path --
run manually.
"""

import asyncio
import json
import statistics

from sqlalchemy import select

from infra.db.session import async_session
from models.signal_token import SignalToken
from models.signal_event import SignalEvent, Milestone

_NON_WIN = (Milestone.ENTRY, Milestone.ARCHIVE, Milestone.DUMP)


def _lane(breakdown: dict) -> str:
    return "REVIVAL" if "drawdown_pct" in breakdown else "FRESH"


def _bucket(ath: float) -> str:
    if ath >= 30.0:
        return ">=30x (3000%+)"
    if ath >= 10.0:
        return "10x-30x"
    if ath >= 5.0:
        return "5x-10x"
    if ath >= 2.0:
        return "2x-5x"
    return "<2x"


async def main():
    async with async_session() as session:
        result = await session.execute(
            select(SignalToken).where(SignalToken.chain == "robinhood")
        )
        signals = result.scalars().all()
        if not signals:
            print("No Robinhood Chain signals found.")
            return
        ids = [s.id for s in signals]
        ev_result = await session.execute(
            select(SignalEvent).where(SignalEvent.signal_id.in_(ids))
        )
        events = ev_result.scalars().all()

    events_by_signal = {}
    for e in events:
        events_by_signal.setdefault(e.signal_id, []).append(e.milestone_type)

    rows = []
    for s in signals:
        breakdown = {}
        if s.entry_breakdown_json:
            try:
                breakdown = json.loads(s.entry_breakdown_json)
            except Exception:
                breakdown = {}
        mtypes = events_by_signal.get(s.id, [])
        win_events = [m for m in mtypes if m not in _NON_WIN]
        dumped = Milestone.DUMP in mtypes
        ath = s.ath_multiple if s.ath_multiple is not None else (s.current_multiple or 1.0)
        rows.append({
            "symbol": s.symbol or s.contract[:10],
            "lane": _lane(breakdown) if breakdown else "UNKNOWN",
            "ath_multiple": ath,
            "entry_market_cap": s.entry_market_cap,
            "drawdown_pct": breakdown.get("drawdown_pct"),
            "recovery_pct": breakdown.get("recovery_pct"),
            "entry_score": s.entry_score,
            "died_before_milestone": len(win_events) == 0,
            "dumped": dumped,
            "milestone_count": len(win_events),
        })

    total = len(rows)
    print("=" * 78)
    print(f"ROBINHOOD LANE-SPLIT ANALYSIS -- {total} signals")
    print("=" * 78)

    by_lane = {}
    for r in rows:
        by_lane.setdefault(r["lane"], []).append(r)

    print("\n-- Lane counts --")
    for lane, rs in sorted(by_lane.items()):
        print(f"  {lane:<10} {len(rs)} signals ({100*len(rs)/total:.1f}%)")

    print("\n-- ath_multiple bucket x lane (counts) --")
    buckets = [">=30x (3000%+)", "10x-30x", "5x-10x", "2x-5x", "<2x"]
    lanes = sorted(by_lane.keys())
    header = f"{'bucket':<18}" + "".join(f"{l:>10}" for l in lanes) + f"{'total':>10}"
    print(header)
    for b in buckets:
        counts = []
        for lane in lanes:
            c = sum(1 for r in by_lane[lane] if _bucket(r["ath_multiple"]) == b)
            counts.append(c)
        print(f"{b:<18}" + "".join(f"{c:>10}" for c in counts) + f"{sum(counts):>10}")

    print("\n-- Died-before-first-milestone rate, by lane --")
    for lane, rs in sorted(by_lane.items()):
        died = sum(1 for r in rs if r["died_before_milestone"])
        dumped = sum(1 for r in rs if r["dumped"])
        print(f"  {lane:<10} died_before_milestone={died}/{len(rs)} ({100*died/len(rs):.1f}%)  "
              f"dumped={dumped}/{len(rs)} ({100*dumped/len(rs):.1f}%)")

    print("\n-- Entry market cap: avg/median, by lane and by outcome --")
    for lane, rs in sorted(by_lane.items()):
        mcs = [r["entry_market_cap"] for r in rs if r["entry_market_cap"]]
        good_mcs = [r["entry_market_cap"] for r in rs if r["entry_market_cap"] and r["ath_multiple"] >= 2.0 and not r["dumped"]]
        bad_mcs = [r["entry_market_cap"] for r in rs if r["entry_market_cap"] and (r["ath_multiple"] < 1.3 or r["dumped"])]
        print(f"  {lane}: all avg=${statistics.mean(mcs):,.0f} med=${statistics.median(mcs):,.0f}" if mcs else f"  {lane}: no MC data")
        if good_mcs:
            print(f"    GOOD (>=2x, no dump): avg=${statistics.mean(good_mcs):,.0f} med=${statistics.median(good_mcs):,.0f} n={len(good_mcs)}")
        if bad_mcs:
            print(f"    BAD  (<1.3x or dump): avg=${statistics.mean(bad_mcs):,.0f} med=${statistics.median(bad_mcs):,.0f} n={len(bad_mcs)}")

    print("\n-- REVIVAL lane only: drawdown_pct vs ath_multiple --")
    rev = by_lane.get("REVIVAL", [])
    if rev:
        xs = [r["drawdown_pct"] for r in rev if r["drawdown_pct"] is not None]
        ys_paired = [(r["drawdown_pct"], r["ath_multiple"]) for r in rev if r["drawdown_pct"] is not None]
        if len(ys_paired) >= 5:
            xs2, ys2 = zip(*ys_paired)
            try:
                corr = statistics.correlation(xs2, ys2)
                print(f"  correlation(drawdown_pct, ath_multiple) = {corr:.3f}  n={len(ys_paired)}")
            except Exception as e:
                print(f"  correlation failed: {e}")
        deep = [r for r in rev if r["drawdown_pct"] and r["drawdown_pct"] >= 60]
        shallow = [r for r in rev if r["drawdown_pct"] and r["drawdown_pct"] < 60]
        for label, grp in (("deep (>=60% drawdown)", deep), ("shallow (<60% drawdown)", shallow)):
            if grp:
                died = sum(1 for r in grp if r["died_before_milestone"])
                avg_ath = statistics.mean([r["ath_multiple"] for r in grp])
                print(f"  {label}: n={len(grp)} died_before_milestone={died}/{len(grp)} avg_ath={avg_ath:.2f}x")
    else:
        print("  No REVIVAL-lane signals found.")

    print("\n" + "=" * 78)
    print("Top 15 by ath_multiple (all lanes):")
    for r in sorted(rows, key=lambda r: -r["ath_multiple"])[:15]:
        dd = f"{r['drawdown_pct']:.0f}%" if r["drawdown_pct"] is not None else "n/a"
        print(f"  {r['symbol']:<12} lane={r['lane']:<8} ath={r['ath_multiple']:.2f}x  "
              f"entry_mc={'$' + format(r['entry_market_cap'], ',.0f') if r['entry_market_cap'] else 'n/a':<14}  "
              f"drawdown={dd:<6}  score={r['entry_score']}  milestones_hit={r['milestone_count']}")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
