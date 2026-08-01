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
- Wallet re-scoring (`engines/scoring.py`) is pure and cheap; run it as a
  scheduled batch job over all `APPROVED` + `PENDING_REVIEW` wallets rather
  than per-event.
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
