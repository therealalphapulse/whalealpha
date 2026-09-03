# Signal Engine Re-Evaluation — Deliverable Summary

Scope: `domain/signals/scoring.py`, `domain/signals/quota.py`,
`domain/signals/pump_radar.py` (confirmations/card only),
`domain/signals/signal_calibration.py`, `domain/signals/signal_tracker.py`.
Holder Intelligence itself (`domain/intelligence/holders.py`) was **not**
touched, per instruction — it was reviewed as an input to confirm what
"reliable" now means for the scorer.

## 0. Did incomplete holder data cause the previous score ceiling? (partially, yes)

Production logs (cited in `pump_radar.py`) showed `base_score` clustering
in the 45-77 range, well short of the 100-point base scale. Two real
mechanisms fed that ceiling, both about to be found and fixed here:

1. **A universal freebie masquerading as neutral scoring.** Every
   candidate — regardless of data quality — received an unconditional
   +2.5 in `_score_wallet_behavior()` ("neutral" was the intent, but the
   code gave a flat bonus, not a zero). That's not a ceiling effect, it's
   noise baked into every score, and it had to go before any ceiling
   analysis could mean anything.
2. **Real signal was being discarded, not just delayed.** When
   `holder_analysis_status == "unavailable_early_token"` (a real,
   documented gap for brand-new Pump.fun mints — RPC succeeds but no
   holder accounts exist yet), bundle detection was correctly withheld.
   But a *resolved*, confirmed-clean dev wallet (`dev_holding_pct` near
   zero) was scored identically to an *unknown* dev wallet — both = 0
   extra points. Now that `get_holder_analysis()` reliably resolves a
   real number whenever `dev_address` is known (not just for early
   tokens — for the general case), that distinction is real and
   scoreable, and wasn't being used.

Both are now fixed (see §1). Neither was "the data literally wasn't
there" in the way the RPC-failover work already solved — that fix made
holder data *available* more often; this fix makes the scorer actually
*use* what's available instead of averaging it away with either a flat
bonus or a missed-credit gap.

## 1. Score weighting changes

| Change | File / function | Why |
|---|---|---|
| Unconditional +2.5 wallet-behavior bonus → conditional on `holder_analysis_status == "ok"` | `_score_wallet_behavior` | Removes score inflation with zero signal behind it; ties the credit to "we actually have real wallet visibility into this token." |
| New +2 bonus for confirmed `dev_holding_pct < 2%` | `_score_wallet_behavior` | Rewards *verified* clean dev wallets, previously scored the same as *unknown* dev wallets. Capped low — a nudge, not a rescue. |
| Explanatory notes added to every previously-silent partial-credit branch (liquidity ratio, top-holder %, top-10 %, buy pressure, volume/mcap ratio, trend agreement, bundle exposure) | `_score_liquidity_integrity`, `_score_holder_distribution`, `_score_momentum_quality`, `_score_wallet_behavior` | Pure explainability fix — no point values changed. Every point on a card now has a stated reason, not just the top band. |
| New `pump_probability` composite (0-100%), reported but **not** scored | `_estimate_pump_probability` (new) | The "Pump Probability" deliverable. Blends momentum/liquidity/holder/smart-money quality + the existing graduation heuristic. Kept out of `final_score`/bonus stack deliberately — adding a second uncapped bonus channel would undermine the `MAX_MULTIPLIER` fix already in place. |

Net effect on the point *ceiling*: unchanged (each category's `min(..., cap)` cap is the same as before). Net effect on the point *floor for the same input*: candidates with genuinely clean, verified data now score at or above where they did before; candidates with no real data lose the old free 2.5. Genuinely weak/unknown candidates cannot score higher than before — nothing here loosens standards.

## 2. Confidence changes

- Added two new confirmation checks in `pump_radar.analyze_candidate`'s `confirmations` dict, sourced from the reliable holder layer: `holder_data_verified` (True when status is `"ok"`, else `None` — never penalized for a legitimately-unknown early token) and `dev_holding_confirmed_low`.
- `MIN_CONFIRMATIONS_REQUIRED`: **2 → 3**. When the gate was designed there were only 5 possible checks; requiring 2 was "a couple out of a small pool." There are now 7. `compute_confidence()` already caps the requirement at `checked_count`, so this is safe for thin-data candidates — it only bites when there's enough resolved data to reasonably ask for more agreement.

## 3. Threshold / dynamic-cutoff changes

- **Signal Tiers redesigned** to the requested 5-tier shape, extended to stay consistent with the existing quota-governor mechanics instead of contradicting them:

  | Score | Tier |
  |---|---|
  | 95+ | 🌟 LEGENDARY |
  | 90–94 | 💎 ELITE |
  | 85–89 | 🟢 HIGH CONVICTION |
  | 80–84 | 🟡 WATCHLIST |
  | 70–79 | 🔵 MARGINAL — only alerted when the quota governor has lowered the live cutoff into this band on a quiet day |
  | 65–69 | ⚪ SUB-FLOOR — counted toward quota-governor supply data, never alerted |
  | <65 | ❌ REJECT — never eligible |

  80 and 70 were chosen to exactly match `quota.DEFAULT_CUTOFF` and `quota.DYNAMIC_FLOOR`, which already existed and were already data-justified in that module's own comments (the daily 100-150 target, `ADJUST_STEP`/`LOOKBACK_DAYS` rolling-average logic) — I didn't invent new cutoffs, I made the tier labels honestly reflect the cutoffs that already govern sending.
- Hard-reject numeric thresholds (30% single wallet, 40% bundle, 15% dev holding, MC/LP ratios) reviewed and **left unchanged** — no backtest data yet exists to justify moving them, and this codebase's own convention (see the graduation-probability docstring) is not to ship confident-looking precision without validation data behind it. The calibration fix in §5 is what will eventually generate that data.
- `quota.py` unchanged numerically; added a comment tying `DEFAULT_CUTOFF`/`DYNAMIC_FLOOR` explicitly to the new tier bands so the two can't drift apart silently in a future edit.

## 4. Risk weighting

No changes to `risk_engine.py` (fake-volume, wash-trading, sniper-wallet, liquidity-lock estimators) — out of scope and already consumed correctly by the scorer. The one risk-relevant change is confidence-side: `dev_holding_confirmed_low` now feeds the confidence gate as real corroboration, and a confirmed-low dev holding earns a small scoring credit (§1), which is the intended way "now-reliable" data should influence risk posture — through an explainable, capped channel, not a threshold change.

## 5. Validation

Found and fixed a dead validation pipeline: `models/signal_token.py` has had an `entry_breakdown_json` column, and `signal_calibration.py` has had a real Pearson-correlation report (`analyze_score_calibration`) reading it, but **nothing ever wrote to it** — `signal_tracker.create_signal_from_candidate` never populated the column, so `sample_size` was permanently 0 and precision/false-positive tracking was impossible no matter how much you ran it. Fixed in `signal_tracker.py` by persisting the exact breakdown dict `score_candidate()` already computes. Also added `smart_money_whale_bonus` and `graduation_bonus` to `signal_calibration.BREAKDOWN_COMPONENTS`, which were already in every breakdown snapshot but excluded from the correlation report.

This means: starting from the next signals sent after this change, `analyze_score_calibration()` will actually accumulate real `(component_score, ath_multiple)` pairs, and once `MIN_SAMPLE_SIZE` (15) is reached, it will report which components are genuinely predictive — the concrete, data-driven way to eventually tune weights and hard-reject thresholds, rather than guessing.

## 6. Expected improvement in signal quality

- **Better differentiation among genuinely strong candidates.** Tokens with real, verified clean holder/dev data will now separate from tokens the pipeline simply hasn't resolved yet, instead of both landing in the same "0 extra points" bucket.
- **Every alert is more explainable.** No more silent partial-credit — the "why" line and score breakdown reflect every contributing signal, not just the strongest bands.
- **Tier labels now mean something concrete** and map 1:1 to the existing send/no-send mechanics, instead of a 4-band scheme (topping out at 90) that didn't leave room to distinguish an exceptional signal from a merely-strong one.
- **The system can finally grade itself.** Calibration reporting was silently broken; it now isn't. This is the single highest-leverage change for future tuning — everything else in this deliverable was reasoned from first principles because no outcome data existed yet.

## 7. Trade-offs introduced

- **Slightly stricter confidence gate** (`MIN_CONFIRMATIONS_REQUIRED` 2→3) will reject a small number of candidates that would have passed before, specifically ones with several resolvable checks that don't agree. This is intentional — "quality over quantity" — but is a real reduction in alert volume on the margin, not just a relabeling.
- **Removing the flat +2.5 wallet-behavior bonus** slightly lowers `base_score` for candidates whose holder data is still an early-token placeholder, even if every other signal about them is excellent. This is also intentional (no more free points for missing data) but means very early, high-quality launches may take an extra scan cycle to clear the floor once real holder data resolves, rather than clearing it immediately on hype alone.
- **`pump_probability` is a heuristic, not a calibrated model** — same caveat as the existing `graduation_probability`. It's explicitly labeled as such everywhere it's shown so it isn't mistaken for a backtested prediction.
- **Hard-reject thresholds are unchanged**, meaning any residual false-positive/false-negative issues at those specific boundaries (30/40/15%) persist until real calibration data (§5) exists to justify moving them — deliberately choosing "don't guess" over "tune blind."
