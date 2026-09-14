"""
scripts/analyze_robinhood_signals.py

One-off / re-runnable analysis: pulls every Robinhood Chain signal ever
sent (SignalToken rows with chain="robinhood") together with every
milestone event fired for it (SignalEvent: 2x/3x/.../10x/dump/archive),
and reports which entry-time features actually distinguished tokens
that went on to perform well from tokens that flopped or dumped.

This exists so scoring-threshold changes are grounded in this bot's own
history instead of general industry conventions (see the code comment
on SignalToken.entry_breakdown_json referencing this exact idea).

Not wired into any routine startup/deploy path -- run manually:
    python scripts/analyze_robinhood_signals.py
"""

import asyncio
import json
import statistics

from sqlalchemy import select

from infra.db.session import async_session
from models.signal_token import SignalToken
from models.signal_event import SignalEvent, Milestone

GOOD_ATH_MULTIPLE = 2.0   # doubled at some point after alert
BAD_ATH_MULTIPLE = 1.3    # never meaningfully moved


def _pearson(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 5:
        return None
    xs2, ys2 = zip(*pairs)
    try:
        return statistics.correlation(xs2, ys2)
    except Exception:
        return None


def _fmt(v, nd=2):
    return "n/a" if v is None else f"{v:.{nd}f}"


async def main():
    async with async_session() as session:
        result = await session.execute(
            select(SignalToken).where(SignalToken.chain == "robinhood")
        )
        signals = result.scalars().all()

        if not signals:
            print("No Robinhood Chain signals found in signal_tokens yet.")
            return

        ids = [s.id for s in signals]
        ev_result = await session.execute(
            select(SignalEvent).where(SignalEvent.signal_id.in_(ids))
        )
        events = ev_result.scalars().all()

    events_by_signal = {}
    for e in events:
        events_by_signal.setdefault(e.signal_id, []).append(e.milestone_type)

    milestone_counts = {}
    for mtypes in events_by_signal.values():
        for m in mtypes:
            key = m.value if isinstance(m, Milestone) else str(m)
            milestone_counts[key] = milestone_counts.get(key, 0) + 1

    rows = []
    for s in signals:
        mtypes = {
            (m.value if isinstance(m, Milestone) else str(m))
            for m in events_by_signal.get(s.id, [])
        }
        dumped = "dump" in mtypes
        ath = s.ath_multiple if s.ath_multiple is not None else (s.current_multiple or 1.0)

        mc_liq_ratio = None
        if s.entry_market_cap and s.entry_liquidity:
            mc_liq_ratio = s.entry_market_cap / s.entry_liquidity

        breakdown = {}
        if s.entry_breakdown_json:
            try:
                breakdown = json.loads(s.entry_breakdown_json)
            except Exception:
                breakdown = {}

        rows.append({
            "contract": s.contract,
            "symbol": s.symbol,
            "ath_multiple": ath,
            "dumped": dumped,
            "good": (ath >= GOOD_ATH_MULTIPLE) and not dumped,
            "bad": (ath < BAD_ATH_MULTIPLE) or dumped,
            "entry_score": s.entry_score,
            "entry_market_cap": s.entry_market_cap,
            "entry_liquidity": s.entry_liquidity,
            "mc_liq_ratio": mc_liq_ratio,
            "dev_holding_pct": s.dev_holding_pct,
            "top_holder_pct": s.top_holder_pct,
            "top10_holder_pct": s.top10_holder_pct,
            "top25_holder_pct": s.top25_holder_pct,
            "bundle_pct": s.bundle_pct,
            "bundle_wallet_count": s.bundle_wallet_count,
            "total_holders": s.total_holders,
            "milestones": sorted(mtypes),
            "breakdown": breakdown,
        })

    total = len(rows)
    good = [r for r in rows if r["good"]]
    bad = [r for r in rows if r["bad"]]
    mid = [r for r in rows if not r["good"] and not r["bad"]]

    print("=" * 72)
    print(f"ROBINHOOD CHAIN SIGNAL HISTORY -- {total} signals total")
    print("=" * 72)
    print(f"GOOD  (ath_multiple >= {GOOD_ATH_MULTIPLE}x, no dump): {len(good)} ({100*len(good)/total:.1f}%)")
    print(f"BAD   (ath_multiple < {BAD_ATH_MULTIPLE}x, or dumped): {len(bad)} ({100*len(bad)/total:.1f}%)")
    print(f"MID   (in between):                        {len(mid)} ({100*len(mid)/total:.1f}%)")
    print()
    print("Milestone events fired, all-time:")
    for k in ("25pct", "50pct", "75pct", "2x", "3x", "4x", "5x", "6x", "10x", "multi_x", "dump", "archive"):
        if k in milestone_counts:
            print(f"  {k:>8}: {milestone_counts[k]}")
    print()

    features = [
        ("entry_score", "Entry score"),
        ("entry_market_cap", "Entry market cap ($)"),
        ("entry_liquidity", "Entry liquidity ($)"),
        ("mc_liq_ratio", "MC:Liquidity ratio"),
        ("dev_holding_pct", "Dev holding %"),
        ("top_holder_pct", "Top holder %"),
        ("top10_holder_pct", "Top 10 holders %"),
        ("bundle_pct", "Bundle wallet %"),
        ("bundle_wallet_count", "Bundle wallet count"),
        ("total_holders", "Total holders"),
    ]

    print("-" * 72)
    print(f"{'Feature':<24}{'GOOD avg':>12}{'BAD avg':>12}{'GOOD med':>12}{'BAD med':>12}")
    print("-" * 72)
    for key, label in features:
        good_vals = [r[key] for r in good if r[key] is not None]
        bad_vals = [r[key] for r in bad if r[key] is not None]
        g_avg = statistics.mean(good_vals) if good_vals else None
        b_avg = statistics.mean(bad_vals) if bad_vals else None
        g_med = statistics.median(good_vals) if good_vals else None
        b_med = statistics.median(bad_vals) if bad_vals else None
        print(f"{label:<24}{_fmt(g_avg):>12}{_fmt(b_avg):>12}{_fmt(g_med):>12}{_fmt(b_med):>12}")

    print()
    print("-" * 72)
    print("Correlation with ath_multiple (all signals, -1..1, None if <5 samples with both values):")
    print("-" * 72)
    ath_values = [r["ath_multiple"] for r in rows]
    for key, label in features:
        xs = [r[key] for r in rows]
        corr = _pearson(xs, ath_values)
        print(f"  {label:<24}{_fmt(corr, 3) if corr is not None else 'n/a':>10}")

    # Breakdown sub-scores, if present on enough rows
    breakdown_keys = set()
    for r in rows:
        breakdown_keys.update(r["breakdown"].keys())
    if breakdown_keys:
        print()
        print("-" * 72)
        print("Entry score breakdown components vs ath_multiple:")
        print("-" * 72)
        for bk in sorted(breakdown_keys):
            xs = [r["breakdown"].get(bk) for r in rows]
            corr = _pearson(xs, ath_values)
            good_vals = [r["breakdown"].get(bk) for r in good if r["breakdown"].get(bk) is not None]
            bad_vals = [r["breakdown"].get(bk) for r in bad if r["breakdown"].get(bk) is not None]
            g_avg = statistics.mean(good_vals) if good_vals else None
            b_avg = statistics.mean(bad_vals) if bad_vals else None
            print(f"  {bk:<22} corr={_fmt(corr,3) if corr is not None else 'n/a':>7}  good_avg={_fmt(g_avg):>8}  bad_avg={_fmt(b_avg):>8}")

    print()
    print("=" * 72)
    print("Worst 10 by ath_multiple (lowest):")
    for r in sorted(rows, key=lambda r: r["ath_multiple"])[:10]:
        print(f"  {r['symbol'] or r['contract'][:10]:<12} ath={r['ath_multiple']:.2f}x  score={_fmt(r['entry_score'],1)}  "
              f"mc_liq={_fmt(r['mc_liq_ratio'],1)}  dev%={_fmt(r['dev_holding_pct'],1)}  top10%={_fmt(r['top10_holder_pct'],1)}  "
              f"milestones={r['milestones']}")
    print()
    print("Best 10 by ath_multiple (highest):")
    for r in sorted(rows, key=lambda r: -r["ath_multiple"])[:10]:
        print(f"  {r['symbol'] or r['contract'][:10]:<12} ath={r['ath_multiple']:.2f}x  score={_fmt(r['entry_score'],1)}  "
              f"mc_liq={_fmt(r['mc_liq_ratio'],1)}  dev%={_fmt(r['dev_holding_pct'],1)}  top10%={_fmt(r['top10_holder_pct'],1)}  "
              f"milestones={r['milestones']}")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
