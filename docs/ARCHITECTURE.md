# Architecture

## Data flow

```
[Indexer/Webhook] -> engines.monitor.ingest_wallet_buy_event()
                          |  (only for APPROVED whale wallets)
                          v
                    EventBuffer (in-memory / Redis)
                          |
              engines.scheduler (every 30s)
                          |
              engines.signal.evaluate_token_cluster()
                          |  (needs min_wallets within window_minutes,
                          |   score >= min_confidence)
                          v
                       Signal (persisted)
                          |
        +-----------------+-----------------+
        v                                     v
notification service                  auto_trading engine
(push to subscribed users)           .process_signal_for_auto_trading()
                                              |
                                    risk.evaluate_auto_trade()
                                    (per-user rules: exposure, slippage,
                                     liquidity, blacklist, cooldown...)
                                              |  approved
                                              v
                          NEW: write Trade row (PENDING -> SUBMITTED)
                                              |
                                    trade_executor.execute_trade()
                                    (Jupiter quote -> sign -> submit -> confirm)
                                              |
                          NEW: reconciliation.py sweeps any trade left
                          PENDING/SUBMITTED across a process restart
```

## Why the admin/user boundary is enforced where it is

`WhaleWalletAdminService` is the single mutation path for the whale database.
Bot commands, any future HTTP admin API, and scripts must all call into it
rather than touching the ORM session directly — that's a deliberate choke
point so "users can never add/edit whale wallets" is one property to audit,
not N scattered checks that can drift out of sync.

## Why Auto Trading is signal-gated, not wallet-gated

`ingest_wallet_buy_event` only ever writes to the event buffer — it has no
path to `trade_executor`. The only route into execution is
`auto_trading.process_signal_for_auto_trading`, which requires a `Signal`
object that already passed `signal`'s multi-wallet confidence threshold. A
single wallet buying a token, however large, cannot trigger a trade by
itself.

## Whale Wallet Discovery & Intelligence Engine

Before this feature, `whale_wallets` only ever grew via a human admin's
`/addwhale` — there was no automated path, and nothing kept the tracked
population within a healthy size or pruned wallets that stopped performing.
`engines/discovery.py` closes that gap with a four-stage cycle, run on a
timer (`DISCOVERY_INTERVAL_SECONDS`, default 15 min):

1. **Source** (`discover_candidates`) — runs **two independent streams**
   each cycle and queues genuinely-new addresses into the `WalletCandidate`
   staging table (never touches `whale_wallets` directly):
   - *Signal-derived* — other large holders of tokens that recently produced
     a real `Signal` (i.e. multiple *already-tracked* whales just
     accumulated it), via plain Solana RPC
     (`find_candidates_from_token_holders`). Higher-precision (co-buyers of
     tokens our own whales picked), but requires at least one tracked wallet
     to have already produced a Signal — it cannot run from zero.
   - *Trending-token bootstrap* — Jupiter's platform-wide trending/most-
     traded tokens (`find_candidates_from_trending_tokens`,
     `DISCOVERY_TRENDING_*` config), independent of anything already
     tracked. **This is what lets the engine find its first wallets with
     zero admin seeding** — without it, `zero tracked wallets -> zero
     ingested buy events -> zero Signals -> zero candidates` is a closed
     loop with no way in, which is exactly the "No approved whale wallets
     yet" state a fresh deploy starts in. It keeps running alongside the
     first stream afterwards too, as an ongoing second discovery channel,
     so the tracked set doesn't only ever grow from wallets correlated with
     what's already tracked. Needs `JUPITER_API_KEY` (or falls back to
     `PRICE_FEED_API_KEY`) — without either, this stream is skipped (a
     warning is logged) and only the signal-derived stream runs.
2. **Evaluate** (`evaluate_candidates`) — fetches each queued candidate's
   swap history (requires `HELIUS_API_KEY`; see below), computes
   FIFO-matched realized PnL/ROI/win-rate/drawdown
   (`engines/discovery_metrics.py`), scores the result with the **same,
   unmodified** `engines/scoring.score_wallet` used for admin-added wallets,
   and runs it through `evaluate_promotion` — a pure function that gates on
   score, ROI, win rate, trade count, wallet age, and wash-trading flags.
   Candidates that clear every gate are promoted straight to `APPROVED` via
   `WhaleWalletAdminService.promote_candidate`.
3. **Re-score & retire** (`rescore_tracked_wallets`) — periodically
   re-fetches history for already-`APPROVED` wallets (oldest-scored first)
   and retires ones that go dormant (`last_active_at` — already populated by
   the existing webhook → `monitor.py` path, no extra fetch needed) or stay
   below the approval bar for `DISCOVERY_LOW_SCORE_CYCLES_BEFORE_RETIRE`
   consecutive cycles (hysteresis, so one noisy cycle can't flip a good
   wallet out). Low-score retirement is suppressed while the tracked
   population is already at/below `DISCOVERY_MIN_TRACKED_WALLETS` — removing
   a mediocre wallet when we're short on wallets would make the shortage
   worse, not better. Inactivity-based retirement is never suppressed.
4. **Enforce the ceiling** (`enforce_population_ceiling`) — if a burst of
   promotions pushes the population over `DISCOVERY_MAX_TRACKED_WALLETS`,
   retires the lowest-scoring wallets back down to it.

Every promotion/retirement *decision* is a pure function
(`evaluate_promotion` / `evaluate_retention` /
`select_wallets_to_retire_for_ceiling` in `engines/discovery.py`) — no DB,
no network — so the admission-control logic is unit-tested directly (see
`tests/unit/test_discovery.py`), the same testability shape as
`engines/scoring.py` and `engines/signal.py`.

**Every write still goes through `WhaleWalletAdminService`, the same choke
point `/addwhale` uses** (see "Why the admin/user boundary is enforced where
it is" below) — the discovery engine acts as a dedicated system user with
`SUPERADMIN` role, so "only administrators may add/remove whale wallets"
stays true even though no human is in the loop for the common case. Every
automated action is audit-logged like any admin action, tagged
`WHALE_WALLET_AUTO_DISCOVERED` / `WHALE_WALLET_SCORE_UPDATE` /
`WHALE_WALLET_STATUS_CHANGE` so it's distinguishable from a human admin's
`/addwhale`/`/approvewhale` in the audit trail.

**Two independent API keys gate two independent things, and it's easy to
have one without the other:**
- **`JUPITER_API_KEY`** (or `PRICE_FEED_API_KEY` as a fallback) gates the
  trending-token bootstrap *source* (step 1b above) — without it, the
  signal-derived stream still runs, but a fresh deploy with zero tracked
  wallets will never find its first candidate.
- **`HELIUS_API_KEY`** gates *scoring* (step 2) — without it, candidates get
  queued (by whichever source is working) but never scored, so nothing gets
  promoted regardless of how many candidates are found. Inactivity-based
  retirement (step 3) doesn't need it and still works.

Running with neither key means the tracked population can only shrink
(inactivity retirement); with only `HELIUS_API_KEY` it can score/retire but
never bootstrap from empty; with only `JUPITER_API_KEY` it can find
candidates but never actually promote any. Set both for the engine to
actually maintain the 500–1500 target end to end.

## Why a PENDING Trade row is written before execution (new vs. the original)

The original TS `tradeExecutor.executeTrade` had no durable record between
"decided to trade" and "transaction confirmed" — a crash in that window left
no trace. This port's `auto_trading.process_signal_for_auto_trading` creates
a `PENDING` `Trade` row before calling the executor, and `trade_executor.py`
promotes it to `SUBMITTED` (recording the blockhash used) immediately before
broadcasting, then to `CONFIRMED`/`FAILED` after the RPC confirms. On
startup, `engines/reconciliation.py` sweeps any row still `PENDING` or
`SUBMITTED` and resolves it against Solana. See `PORTING_NOTES.md` item #3
for the full reasoning and the states this handles.

## Scaling notes

- The event buffer and scheduler are intentionally simple (in-memory,
  `asyncio` sleep loop) so the logic is easy to read and test. Before running
  multiple workers, move the buffer to Redis sorted sets and the evaluation
  loop to an `arq` cron job, so only one worker instance handles a given
  token cluster.
- Wallet re-scoring (`engines/scoring.py`) is pure and cheap; it now runs as
  a scheduled batch job over `APPROVED` wallets via
  `engines/discovery.rescore_tracked_wallets`, capped per cycle by
  `DISCOVERY_RESCORE_BATCH_SIZE` rather than per-event.
- `Signal` and `WalletEvent` tables are indexed on
  `(token_mint, created_at/observed_at)` for the query patterns the
  scheduler and bot commands use.

## What's stubbed vs. real

The diagram above was aspirational when first written — the notification and
auto-trading branches existed as TODOs with no caller. As of this port,
that's closed: `engines/scheduler.py` actually calls
`services.notification.notify_signal_subscribers` and
`engines.auto_trading.process_signal_for_auto_trading`, and the USD->lamports
conversion uses a real price feed (`integrations/price_feed.py`) instead of
the old `/ 1` placeholder. See README.md's "Feature status" table for the
full list of what's wired up.

Still-open `TODO(integration)` markers:
- `integrations/solana_connection.py` — bulk wallet monitoring at scale
  (polling vs. subscribing) once you're past a handful of tracked wallets
- `engines/scheduler.py` — token safety context (liquidity/holders/LP lock)
  is still passed as `None` to `evaluate_token_cluster`; wire in a
  liquidity/holder-data provider to score signals less conservatively
- `integrations/jupiter_client.py` — signing flow (custodial vs
  non-custodial); the custodial encrypted-key signer in
  `engines/trade_executor.py` and `bot/commands/wallet.py` is a starting
  point requiring a security review, not a production-ready signer
- `integrations/price_feed.py` and `integrations/helius_webhook.py` — both
  have a real default implementation (Jupiter Price API; Helius enhanced
  webhooks) but are explicitly flagged as assumptions about a third-party
  payload/response shape you should verify against live data

Everything else (scoring math, signal aggregation, risk-rule evaluation,
RBAC, audit logging, encryption, restart-safe reconciliation, and the
notification/auto-trading/manual-trading/wallet-connection/price-alert
wiring) is real, tested logic, not a placeholder.
