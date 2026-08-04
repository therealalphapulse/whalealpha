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

## Whale Wallet Discovery & Intelligence Engine (Hybrid Discovery Engine, Phase 1 refactor)

Before this feature, `whale_wallets` only ever grew via a human admin's
`/addwhale` — there was no automated path, and nothing kept the tracked
population within a healthy size or pruned wallets that stopped performing.
`engines/discovery.py` closes that gap with a five-stage cycle, run on a
timer (`DISCOVERY_INTERVAL_SECONDS`, default 15 min):

1. **Source** (`discover_candidates`) — runs **every enabled discovery
   source in priority order**, drawing from one shared,
   priority-ordered budget (`DISCOVERY_CANDIDATE_BATCH_SIZE` — a
   higher-priority source's unused budget rolls over to the next, rather
   than a fixed split), and queues genuinely-new addresses into the
   `WalletCandidate` staging table (never touches `whale_wallets` directly).
   One source failing or being disabled never blocks the rest:
   - *Real-time on-chain launch discovery* (`integrations/free_market_sources.py`,
     Priority 1) — pump.fun, LaunchLab, Raydium, and Meteora's public,
     keyless "recent launches / new pools" endpoints, each resolved to its
     largest holders via plain Solana RPC
     (`find_candidates_from_token_holders`, no API key). **This is what
     actually eliminates the cold-start loop** — it needs no tracked
     wallets, no Signals, and no paid API key, so it produces candidates
     from a completely empty database.
   - *Trending-token fallback chain* (Priority 2) — Jupiter Tokens API V2
     first (`find_candidates_from_trending_tokens`, needs
     `JUPITER_API_KEY`/`PRICE_FEED_API_KEY`); if that's unavailable, falls
     through to Birdeye's free tier, then DexScreener's fully keyless feed
     (`find_trending_tokens_multi_provider`). Stops at the first provider
     that returns results.
   - *Signal-derived* (Priority 3, legacy stream, unchanged behavior) —
     other large holders of tokens that recently produced a real `Signal`
     (i.e. multiple *already-tracked* whales just accumulated it). Highest
     precision, but by itself can never bootstrap from zero tracked
     wallets — now just one source among several rather than a hard
     dependency.
   - *Wallet Graph Expansion* (Priority 4, `engines/wallet_graph.py`) —
     runs as its own phase later in the cycle (see step 4 below), not
     inside `discover_candidates`, since it needs already-`APPROVED`
     wallets to expand from.
2. **Evaluate** (`evaluate_candidates`) — fetches each queued candidate's
   swap history (requires `HELIUS_API_KEY`; see below), computes
   FIFO-matched realized PnL/ROI/win-rate/drawdown
   (`engines/discovery_metrics.py`, Priority 5), scores the result with the
   **same, unmodified** `engines/scoring.score_wallet` used for admin-added
   wallets, and runs it through `evaluate_promotion` — a pure function that
   gates on score, ROI, win rate, trade count, wallet age, and wash-trading
   flags (all unchanged from before this refactor). In parallel, computes
   on-chain behaviour scores (`engines/behavior_scoring.py`, Priority 6 —
   Early Buyer, Diamond Hand, Quick Flip, Sniper Probability, Conviction,
   Consistency, Risk) and derives smart-money labels from them
   (`engines/wallet_labels.py`, Priority 7). Behaviour only ever *enriches*
   confidence by a small, bounded amount (`behavior_confidence_bonus`,
   capped at ±8) — it never overrides the core score or any promotion gate.
   Candidates that clear every gate are promoted straight to `APPROVED` via
   `WhaleWalletAdminService.promote_candidate`, with their labels copied
   onto `WhaleWallet.tags`.
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
4. **Expand the wallet graph** (`engines/wallet_graph.expand_wallet_graph`,
   Priority 4) — every `APPROVED` wallet is a discovery node: its recently
   traded token mints are re-queried for co-holders, and a related address
   that co-occurs across at least `DISCOVERY_GRAPH_MIN_COOCCURRENCE`
   distinct tokens with an already-trusted wallet is queued as its own
   candidate. Relationship strength is tracked in a new `wallet_relationships`
   table (plain Postgres, not a graph database) via the pure
   `compute_strength`/`update_relationship` functions. Runs incrementally
   over a batch of `APPROVED` wallets per cycle
   (`DISCOVERY_GRAPH_EXPANSION_BATCH_SIZE`), never the whole population at
   once.
5. **Enforce the ceiling** (`enforce_population_ceiling`) — if a burst of
   promotions pushes the population over `DISCOVERY_MAX_TRACKED_WALLETS`,
   retires the lowest-scoring wallets back down to it.

Every promotion/retirement *decision*, plus the wallet-graph strength math,
is a pure function (`evaluate_promotion` / `evaluate_retention` /
`select_wallets_to_retire_for_ceiling` / `wallet_graph.compute_strength` /
`wallet_graph.update_relationship`) — no DB, no network — so the
admission-control and graph logic is unit-tested directly (see
`tests/unit/test_discovery.py`, `tests/unit/test_wallet_graph.py`,
`tests/unit/test_behavior_scoring.py`, `tests/unit/test_wallet_labels.py`),
the same testability shape as `engines/scoring.py` and `engines/signal.py`.

**Every write still goes through `WhaleWalletAdminService`, the same choke
point `/addwhale` uses** (see "Why the admin/user boundary is enforced where
it is" below) — the discovery engine acts as a dedicated system user with
`SUPERADMIN` role, so "only administrators may add/remove whale wallets"
stays true even though no human is in the loop for the common case. Every
automated action is audit-logged like any admin action, tagged
`WHALE_WALLET_AUTO_DISCOVERED` / `WHALE_WALLET_SCORE_UPDATE` /
`WHALE_WALLET_STATUS_CHANGE` so it's distinguishable from a human admin's
`/addwhale`/`/approvewhale` in the audit trail.

**API keys are all optional, and every source degrades independently:**
- The Priority 1 on-chain launch sources and the DexScreener leg of the
  Priority 2 fallback chain need **no API key at all** — this is what lets
  the engine bootstrap from a completely fresh deploy with zero
  configuration beyond `SOLANA_RPC_URL`.
- **`JUPITER_API_KEY`** (or `PRICE_FEED_API_KEY`) upgrades the Priority 2
  trending source to Jupiter's data; without it, the chain falls through to
  Birdeye (optionally `BIRDEYE_API_KEY` for a higher free-tier ceiling) and
  then DexScreener automatically.
- **`HELIUS_API_KEY`** gates *scoring* (step 2) — without it, candidates get
  queued (by whichever sources are working) but never scored, so nothing
  gets promoted regardless of how many candidates are found. Inactivity-based
  retirement (step 3) doesn't need it and still works.

Set `HELIUS_API_KEY` for the engine to actually promote what it finds; every
other key is a nice-to-have that widens a fallback chain, not a hard
requirement to bootstrap from zero.

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
