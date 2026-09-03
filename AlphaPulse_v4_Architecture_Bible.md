# AlphaPulse v4 — Official Software Architecture Bible

**Status:** Long-term engineering constitution
**Scope:** Evolutionary architecture, not a rewrite
**Precondition:** Every decision below traces back to a specific, verified finding from the AlphaPulse engineering audit. Nothing here is speculative or generic.

---

## 1. Executive Architecture Vision

AlphaPulse v3 is not a prototype pretending to be a product — the audit confirmed real Jupiter-signed on-chain trading, a genuinely well-built multi-RPC failover layer, solid envelope encryption, and a real RBAC system. The problem is not that the engineering is bad. The problem is that **good single-process engineering was asked to do a multi-process job**: every piece of shared state — FSM sessions, the RPC rate limiter, the provider cache, the request dedup table — lives inside the memory of one Python interpreter. That interpreter cannot be replicated, so the system cannot scale, and it cannot be restarted without losing user context.

v4's philosophy is **subtraction of bottlenecks, not replacement of engineering**. Three moves do almost all of the work:

1. **Externalize state that is currently trapped in-process** (FSM, cache, rate limiter, dedup, locks) into Redis, so any number of stateless processes can share it.
2. **Separate what runs on a schedule from what responds to a user** (background intelligence loops vs. Telegram command handling), so a slow scan cycle can never make a user's `/token` command hang, and vice versa.
3. **Generalize the one piece of infrastructure that was already built correctly** — `multi_rpc_manager`'s queue/cache/circuit-breaker design — so it protects *all* outbound provider traffic (market data included, not just RPC), instead of protecting only half of it.

Everything else — the trading engine, the encryption scheme, the RBAC model, the scoring heuristics — is preserved. v4 is a deployment-topology and state-management redesign wrapped around an already-competent domain core, plus the closing of specific, named gaps the audit found (no migrations, no FKs, no tests, no Docker, no observability, two self-flagged-unverified risk gates, one confirmed-dead safety setting, one confirmed non-functional feature).

---

## 2. Layered System Architecture Diagram (Logical)

```
┌──────────────────────────────────────────────────────────────────┐
│  PRESENTATION LAYER — Telegram Bot Gateway  (stateless, N replicas)│
│  aiogram routers · webhook ingress · Redis-backed FSM storage      │
└────────────────────────────┬───────────────────────────────────────┘
                              │  Auth / RBAC / Premium middleware
┌────────────────────────────▼───────────────────────────────────────┐
│  APPLICATION LAYER — Command Handlers                               │
│  thin: parse input → call one domain service → format reply         │
└────────────────────────────┬───────────────────────────────────────┘
                              │
┌────────────────────────────▼───────────────────────────────────────┐
│  DOMAIN / SERVICE LAYER — bounded contexts, no cross-imports        │
│  Signals · Trading (Real + Paper, shared execution contract) ·      │
│  Intelligence (Wallets, KOL, Narrative) · Payments · Admin/RBAC     │
└───────────────┬───────────────────────────────────┬─────────────────┘
                │                                   │
┌───────────────▼───────────────┐    ┌───────────────▼─────────────────┐
│  PROVIDER ABSTRACTION LAYER    │    │  DATA LAYER                      │
│  Provider Gateway Service       │    │  PostgreSQL (Alembic-versioned,  │
│  — one Provider protocol for    │    │  FK-enforced) · Redis (state,    │
│  RPC AND market-data providers  │    │  cache, dedup, locks, queue)     │
└───────────────┬───────────────┘    └───────────────┬─────────────────┘
                │                                    │
┌───────────────▼────────────────────────────────────▼─────────────────┐
│  EXTERNAL SYSTEMS                                                      │
│  Helius / Alchemy / dRPC / QuickNode · DexScreener / GeckoTerminal /   │
│  CoinGecko / GoPlus · Jupiter · Telegram Bot API · KMS (AWS/GCP/Vault) │
└─────────────────────────────────────────────────────────────────────┘
```

**Why this shape, specifically:** the audit found the current codebase already *has* this layering in spirit (`bot/` → `services/` → `models/`) but violates it twice (`pump_radar.py` and `alert_engine.py` importing `bot/keyboards` directly) and has no enforced boundary preventing a third violation tomorrow. v4 keeps the same four logical layers but makes the Provider layer a real architectural tier — today it only exists for RPC calls; market-data calls bypass it entirely, which is the single largest resilience gap the audit identified (§4 of the audit).

---

## 3. Physical Deployment Architecture Diagram

```
                          ┌───────────────────────┐
                          │   Telegram Bot API      │
                          └───────────┬─────────────┘
                                      │ HTTPS webhook
                          ┌───────────▼─────────────┐
                          │  Load Balancer / TLS term │
                          └───────────┬─────────────┘
              ┌───────────────────────┼───────────────────────┐
     ┌────────▼────────┐    ┌─────────▼────────┐    ┌──────────▼────────┐
     │ Bot Gateway #1   │    │ Bot Gateway #2    │    │ Bot Gateway #N     │
     │ stateless aiogram│    │ stateless aiogram  │    │ stateless aiogram  │
     └────────┬────────┘    └─────────┬────────┘    └──────────┬────────┘
              └───────────────────────┼───────────────────────┘
                                      │
                        ┌─────────────▼─────────────┐
                        │        Redis Cluster        │
                        │ FSM state · cache · dedup ·  │
                        │ rate-limit counters · locks  │
                        └─────────────┬─────────────┘
      ┌───────────────────────────────┼───────────────────────────────┐
┌─────▼─────────────┐     ┌───────────▼───────────┐      ┌─────────────▼──────────┐
│ Provider Gateway    │     │ Signal/Trading Worker   │      │ Intelligence Worker      │
│ Service (singleton, │     │ pool — user-facing,     │      │ pool — Premium engine,   │
│ multi_rpc_manager    │     │ latency-sensitive loops │      │ isolated fault domain    │
│ logic, unchanged)    │     │ (scan, alert, DCA, exit)│      │ (wallet discovery/score) │
└─────┬───────────────┘     └───────────┬───────────┘      └─────────────┬──────────┘
      │                                 │                                │
      │                     ┌───────────▼────────────────────────────────▼──┐
      │                     │   PostgreSQL primary (+ read replica at 10k+)  │
      │                     │   PgBouncer transaction-pooled connections     │
      │                     └────────────────────────────────────────────┘
      │
┌─────▼─────────────────────────┐
│ External providers: RPC nodes, │
│ market-data APIs, Jupiter, KMS │
└────────────────────────────────┘

Every box above ships with an OpenTelemetry sidecar → Prometheus/Grafana + Sentry.
CI/CD (GitHub Actions): test → build Docker image → push → deploy to any container host
(Railway, ECS, Cloud Run, k8s — no longer locked to one platform's buildpack).
```

**Why this shape, specifically:** the audit confirmed there is currently **no Dockerfile anywhere in the repo** — deployment is entirely dependent on Railway's Nixpacks buildpack (§8 of the audit), and the bot uses Telegram long-polling with a single `Dispatcher()` (§3/§11). Long-polling cannot be run from more than one process against the same bot token without the two processes fighting over `getUpdates` — this is a hard Telegram API constraint, not a design opinion, and it is the concrete reason the current architecture has a scalability ceiling of exactly one instance. Moving to webhook ingress behind a load balancer is the minimum required change to make the presentation layer replicable at all.

---

## 4. Module Boundary Redesign Plan

The audit's dependency graph showed `services/pump_radar.py` with 25 internal imports and the highest fan-out in the codebase, two confirmed layering violations, and two 1,600+ line "god files" in `bot/commands/`. v4 does not rewrite these modules' logic — it redraws the boundaries around the logic that already exists.

```
alphapulse/
├── platform/                 # was: bot/ (routing infra only)
│   ├── gateway/               # aiogram app, webhook entrypoint, DI wiring
│   ├── middleware/             # NEW: auth, RBAC, premium-gate, correlation-id
│   └── commands/               # thin handlers only — parse, call domain, format
│       ├── real_wallet/        # split from the 1,909-line file: buy, sell, dca,
│       │                       # exits, limit_orders, withdraw — one file each
│       └── paper_trading/      # same split as real_wallet
├── domain/
│   ├── signals/                # was: pump_radar.py, decomposed by stage
│   │   ├── discovery.py        # candidate sourcing
│   │   ├── enrichment.py       # holders, LP-lock, funding graph, deployer history
│   │   ├── scoring.py          # conviction_scorer — UNCHANGED heuristics
│   │   ├── quota.py            # quota_governor — UNCHANGED
│   │   └── alerting.py         # formatting + dispatch
│   ├── trading/
│   │   ├── execution_contract.py  # NEW: shared interface Paper + Real implement
│   │   ├── real/                # real_trade_engine, jupiter_swap — UNCHANGED core
│   │   └── paper/                # paper_engine — UNCHANGED core
│   ├── intelligence/            # premium wallet discovery/scoring, KOL, whales
│   ├── payments/                # solana_payment_verify, premium_service
│   └── admin/                   # admin_rbac — UNCHANGED
├── providers/                   # was: split across services/, now unified
│   ├── protocol.py              # NEW: one Provider interface
│   ├── rpc/                     # Helius/Alchemy/dRPC/QuickNode adapters
│   └── marketdata/              # DexScreener/GeckoTerminal/CoinGecko/GoPlus adapters
├── infra/
│   ├── db/                      # models + Alembic
│   ├── cache/                   # Redis client
│   ├── kms/                     # MasterKeyProvider implementations
│   └── observability/           # tracing, metrics, logging setup
└── models/                       # UNCHANGED column definitions, + FKs, + indexes
```

**Enforcement, not just convention:** a lightweight import-linter rule (or `pytest`-collected static check) runs in CI and fails the build if `domain/` imports from `platform/commands/` or if any module outside `providers/` opens an `aiohttp.ClientSession` directly. This is the structural fix for the exact violation the audit found (`pump_radar.py` → `bot/keyboards`) — v3 had no mechanism to prevent it from happening again; v4 does.

---

## 5. State Management Strategy

| State | v3 (verified) | v4 | Why change is required |
|---|---|---|---|
| FSM (conversation state) | aiogram default `MemoryStorage`, in-process | `RedisStorage` (aiogram's built-in, drop-in swap — no handler code changes) | Confirmed in `main.py`: `Dispatcher()` with no storage arg. A user mid-withdrawal loses all progress on any restart, and state cannot be shared across >1 gateway instance, which horizontal scaling requires. |
| RPC rate limiter / circuit breaker / dedup / TTL cache | In-process objects inside `multi_rpc_manager.py` | **Same code, promoted to a standalone Provider Gateway Service** — one running instance, called by all Bot Gateway and Worker replicas over an internal queue/RPC | The algorithm itself is sound and explicitly preserved per the non-negotiable list. The problem is topology: if this logic is embedded as a library inside N replicated processes, each replica enforces its own rate ceiling independently, silently multiplying the *global* outbound request rate by N — defeating the entire purpose of the limiter. Making it a singleton service (not rewriting its logic) is the minimal fix. |
| Market-data cache | **None** (verified: `dexscreener.py`, `geckoterminal.py`, `coingecko.py` open a fresh `aiohttp.ClientSession` per call, no TTL cache) | Shared Redis-backed cache inside the same Provider Gateway | Audit §4/§7: this is the most consequential provider-layer gap found — the most frequently called external APIs in the bot have zero caching today. |
| Background-loop leadership | N/A — only one process ever runs | Redis-based distributed lock (leader election) per loop | If v4 simply ran `main.py`'s current loop soup on multiple worker replicas unmodified, every DCA/exit/automation loop would execute twice per replica-count — a direct correctness bug (double trades), not just an inefficiency. A lock ensures exactly one replica runs each singleton job at a time. |
| Session/cookie-equivalent (Telegram identity) | Telegram `user_id`, stateless | Unchanged | Already stateless and correct; nothing to fix. |

---

## 6. Provider Abstraction Architecture

The audit's clearest, single most consequential finding in this area: **there are two provider layers today, not one**, and only one of them got the resilience investment.

```python
# providers/protocol.py  (illustrative shape, not a rewrite of adapter logic)
class Provider(Protocol):
    name: str
    async def fetch(self, method: str, params: dict) -> ProviderResult: ...
    def health(self) -> ProviderHealth: ...
```

- **RPC family** (Helius, Alchemy, dRPC, QuickNode): already effectively behind `multi_rpc_manager` — v4 formalizes the existing behavior into the `Provider` interface without touching the queue/backoff/circuit-breaker/dedup logic itself (non-negotiable preservation).
- **Market-data family** (DexScreener, GeckoTerminal, CoinGecko, GoPlus): today each is a standalone module with its own bespoke `aiohttp.ClientSession()`-per-call pattern, no shared cache, no shared rate limiter, no retry (audit §4 table). v4 wraps each in the same `Provider` interface and routes it through the **same Provider Gateway Service** as the RPC family — reusing the proven queue/cache/circuit-breaker engine rather than inventing a second one. This is generalization of existing infrastructure, not new architecture.
- **Field-mapping/business logic inside each adapter is preserved verbatim** — the work is only in the transport/resilience wrapper around it.
- RugCheck: the audit found **no RugCheck integration exists in the codebase** despite being in scope. v4 does not silently assume it should be added — that is a product decision for a future phase, tracked explicitly as a gap, not invented here.

---

## 7. Database Evolution Strategy

Audit findings this section directly answers: no migration framework exists (hand-written `ALTER TABLE IF NOT EXISTS` functions run on every boot, which already caused one documented production incident in `signal_tracker.py`); ~25 models are missing `ForeignKey` constraints on `user_id`/`telegram_id` despite being indexed; no explicit connection-pool sizing.

**Migration framework:**
1. Introduce Alembic. Generate a baseline migration against the *current* production schema (via `alembic revision --autogenerate` pointed at a schema-matching database, reconciled by hand against the model files) so the migration history starts from where the system actually is today — not a re-imagined ideal schema.
2. Retire the hand-written `migrate_*_schema()` functions in `signal_tracker.py`, `kol_tracker.py`, `paper_engine.py`, and `solana_wallet.py` **only after** the baseline migration is proven to reproduce the live schema exactly. They stay in place, inert, until that's verified — no risk taken on a system already handling real funds.
3. All future schema changes go through reviewed Alembic revisions in CI, never through code executed silently at boot.

**Foreign key integrity (phased, not a single risky migration):**
1. Add each missing `ForeignKey(users.telegram_id)` as `NOT VALID` (Postgres feature — enforces on new rows, doesn't scan/lock existing ones).
2. Run a background audit query per table for orphaned rows; alert and manually resolve (soft-delete or re-attach) rather than silently deleting user data.
3. `VALIDATE CONSTRAINT` to convert to a fully-enforced FK once orphan count is confirmed zero.
4. Apply to all ~25 affected models: `paper_trade`, `real_wallet`, `watchlist`, `tracked_wallet`, `portfolio`, `alert`, `premium_membership`, and the rest identified in the audit's model scan.

**Indexing plan:** audit the actual hot query paths (signal lookups by user+status, trade history by user+date, wallet lookups by address) and add composite indexes matched to real `WHERE`/`ORDER BY` clauses, closing the coverage gap the audit found on `alert.py`, `kol_wallet.py`, `pump_alerted_token.py`, and `system_flag.py`.

**Connection management:** explicit `pool_size`/`max_overflow` on the async engine, sized against expected concurrent replica count; introduce PgBouncer in transaction-pooling mode ahead of the 10k-user tier, where the number of stateless Bot Gateway + Worker replicas will otherwise open more direct Postgres connections than a single primary comfortably supports.

**Retention:** a scheduled archival job (already hinted at by the existing `daily_trade_archive.py`) formalized to prune/cold-store `signal_events` and `admin_activity_log` on a defined window, rather than unbounded growth.

---

## 8. Security Architecture (KMS-ready)

The audit found the wallet encryption scheme itself (`wallet_crypto.py`) to be genuinely well-designed — AES-256-GCM envelope encryption, per-wallet random data keys — and its own docstring already names the gap: a single environment variable (`EnvMasterKeyProvider`) is the only thing standing in for a real KMS today.

**The fix is exactly the seam the module was already built for.** `wallet_crypto.py` is designed around a `MasterKeyProvider` abstraction; v4 adds a second implementation of that same interface:

```
infra/kms/
├── env_provider.py     # EXISTING EnvMasterKeyProvider — kept for local/dev only
└── kms_provider.py     # NEW: AWS KMS / GCP KMS / HashiCorp Vault-backed
```

No change to `encrypt()`/`decrypt()` call sites anywhere in `real_trade_engine.py`, `real_dca_engine.py`, etc. — this is a configuration swap, not a rewrite, because the abstraction boundary already exists in the current code.

Additional security work, each tied to a specific audit finding:
- **Structural permission enforcement**: move `has_permission()`/premium checks from per-handler calls (audit §3/§10: correctness currently depends on every handler author remembering to call the check) into `platform/middleware/` — a missing check becomes structurally impossible for a new handler rather than a code-review responsibility.
- **Secrets manager for API keys**, not just the wallet master key — same class of exposure, same fix pattern.
- **Payment verification hardening**: `solana_payment_verify.py` was confirmed real but self-flagged as untested and running a single, non-failover RPC call for a payment-gating function (audit §1). Route it through the Provider Gateway (§6 above) to inherit failover for free, and require a live-transaction test pass before v4 ships it as authoritative.
- **Rate limiting/abuse protection** at the Bot Gateway/load-balancer tier — did not exist in v3 at all.
- **Key rotation process** for both the KMS master key and API keys, documented and drilled, not just theoretically possible.

---

## 9. Scalability Model: 1,000 → 10,000 → 100,000 Users

| Tier | What changes from the tier below | Audit bottleneck being resolved |
|---|---|---|
| **~1,000** (v4.0 baseline) | Webhook ingress + 2 stateless Bot Gateway replicas behind a load balancer; Redis for FSM + cache; single Provider Gateway instance; single Postgres with explicit pool sizing | Removes the single-process ceiling caused by long-polling + `MemoryStorage` (audit §11, root cause #1). |
| **~10,000** | Bot Gateway auto-scales independently of Worker pools; Signal/Trading workers and Intelligence workers run as separate deployments (fault isolation — a slow Premium-engine scan can no longer compete with the event loop handling `/token` commands); PgBouncer in front of Postgres; read replica for reporting/analytics queries | Removes the shared-event-loop contention the audit found between ~10 background loops and user command handling (audit §7); removes the uncached, unrate-limited market-data provider layer as a shared ceiling (audit §4). |
| **~100,000** | Postgres sharded or moved to a managed horizontally-scalable variant if write volume demands it; Redis cluster (not single node) for cache/locks; Provider Gateway horizontally scaled with a shared distributed rate-limit budget (not per-instance) using the same circuit-breaker/backoff algorithm, now backed by Redis counters instead of in-process ones; dedicated read-replica fleet for analytics/reporting workloads separate from transactional traffic | At this scale, the free-tier ceilings on DexScreener/GeckoTerminal/CoinGecko/GoPlus (audit §4/§11) become the dominant constraint regardless of internal architecture — this tier requires a paid/enterprise market-data contract as a business decision, not purely an engineering one; the architecture makes that swap a configuration change, not a redesign. |

---

## 10. Observability Stack Design

The audit found **zero** external error tracking, **zero** structured logging, and **zero** metrics/tracing anywhere in the codebase — only `logging.basicConfig` to stdout and `multi_rpc_manager`'s own internal (but not externally exposed) `provider_stats()`/`queue_depths()` methods.

- **Error tracking:** Sentry (or equivalent) wired at the framework boundary in `platform/gateway/` and in the Provider Gateway — catches the exact class of error the audit found being silently absorbed by ~245 broad `except Exception` blocks, without requiring every one of those blocks to be individually rewritten first.
- **Metrics:** Prometheus exporters built directly on top of the **existing** `multi_rpc_manager.provider_stats()`/`queue_depths()` — this instrumentation was already built in v3, it just was never exposed outside the process. Exposing it is additive, not new engineering.
- **Tracing:** OpenTelemetry spans from Bot Gateway → domain service → Provider Gateway → DB, with a correlation ID generated at webhook ingress and threaded through every downstream call and log line — directly answers the audit's observation that there's currently no way to trace a user's command through to the provider calls it triggered.
- **Structured logging:** JSON log format, correlation-ID-tagged, replacing the plain-text `%(asctime)s | %(levelname)s | %(message)s` format and the 39 stray `print()` calls the audit found bypassing the logging framework entirely.
- **Alerting:** rules on circuit-breaker trips (leveraging existing `multi_rpc_manager` health scoring), DB pool exhaustion, and elevated error rate — closing the audit's finding that today, a human only finds out about a problem by reading logs manually.

---

## 11. Background Job Architecture

Audit finding: `main.py` currently starts roughly 10 concurrent `asyncio.create_task` loops (alerting, KOL sync, signal lifecycle, paper monitor, DCA, automation, exit engine, limit orders, payment sweep, plus 4 more when the Premium engine is enabled) **all sharing the same event loop as user command handling** — with no isolation, and (per §5 above) no protection against duplicate execution if ever run on more than one replica.

v4 splits this into two independently deployable worker pools, replacing the loop soup with a proper scheduler (APScheduler with a Redis jobstore, or equivalent) that supports distributed locking natively rather than assuming single-process execution:

| Worker pool | Jobs (unchanged business logic, moved out of `main.py`) | Isolation rationale |
|---|---|---|
| **Signal/Trading Worker** | scan/alert loop, signal lifecycle, paper monitor, real DCA/exit/limit-order/automation engines | User-facing latency-sensitive; must never be blocked by the slower Intelligence engine, and vice versa (audit §7 finding: everything currently shares one event loop). |
| **Intelligence Worker** | Premium wallet discovery/scoring/maintenance, KOL sync | Already effectively isolated behind the `PREMIUM_BACKGROUND_SCHEDULERS_ENABLED` flag today (audit §1) — v4 formalizes that existing intent as a real deployment boundary instead of an env-var toggle inside a shared process. |

Each singleton job acquires a Redis lock before running; if a lock is held, the replica skips that cycle rather than double-executing — the direct fix for the correctness risk identified in §5.

---

## 12. Refactor Roadmap

**v4.0 — Foundation (must ship before any scaling work is safe)**
- Dockerfile + docker-compose for local dev (closes: no container definition exists today)
- CI pipeline: lint, import-boundary check, test run, image build
- Alembic baseline migration + retirement plan for hand-written migration functions
- Test suite bootstrapped for the highest-risk modules first: `wallet_crypto`, `admin_rbac`, `real_trade_engine`, `conviction_scorer`
- Fix the two confirmed layering violations (`pump_radar.py`, `alert_engine.py` → `bot/keyboards`)
- Resolve the dead `SIGNAL_MIN_LOCKED_LIQUIDITY_PCT` setting — either wire it in for real or delete the misleading "mandatory" documentation
- Live-validate the three self-flagged-unverified modules (`lp_lock_checker.py`, `funding_graph.py`, `deployer_history.py`, `jupiter_price.py`) against real API responses before any of them influence a real-money decision at higher scale

**v4.1 — Externalized State & Fault Isolation**
- Redis-backed FSM storage (drop-in aiogram swap)
- Provider Gateway Service extraction (multi_rpc_manager promoted to standalone service, logic unchanged; market-data providers folded into the same gateway)
- Split Signal/Trading Worker and Intelligence Worker into separate deployments
- Distributed locking for all singleton background jobs
- Move permission/premium checks into middleware

**v4.2 — Horizontal Scale & Production Hardening**
- Webhook ingress + N stateless Bot Gateway replicas behind a load balancer
- FK integrity rollout (phased `NOT VALID` → validate → enforce) across all ~25 affected models
- KMS-backed `MasterKeyProvider` swapped in for production
- Full observability stack (Sentry, Prometheus/Grafana, OpenTelemetry, structured logging)
- PgBouncer + connection pool tuning ahead of the 10k-user tier
- Resolve the KOL-tracking product gap (either ship a real default provider or explicitly gate the feature's UI behind a "not configured" state instead of silently doing nothing)

---

## 13. Risk Assessment of Migration

| Risk | Phase | Mitigation |
|---|---|---|
| FK backfill discovers real orphaned rows (data quality worse than assumed) | v4.2 | `NOT VALID` + audit-before-enforce approach (§7) — never a blind cutover; manual resolution path for every orphan found. |
| Webhook cutover causes a gap in update delivery during the switch from long-polling | v4.2 | Dual-run window: bring up webhook ingress, verify delivery, then disable polling — not a hard cutover. |
| KMS integration mis-wraps or loses access to existing encrypted wallet keys | v4.2 | `MasterKeyProvider` swap is additive (§8) — old `EnvMasterKeyProvider`-encrypted data is re-wrapped under the new provider in a verified, reversible migration step with the old key retained until confirmed. |
| Distributed lock bugs cause a background job to run zero times (missed cycle) instead of double-executing | v4.1 | Missed-cycle is the safe failure mode by design (skip, don't duplicate); alerting (§10) on jobs that haven't run within their expected interval. |
| Provider Gateway extraction becomes a new single point of failure | v4.1 | It replaces an existing single point of failure (the in-process `multi_rpc_manager` singleton already *was* one) — not a new risk, a relocated one; standard service redundancy (multiple replicas behind the same Redis-backed rate state) applies at the 100k tier (§9). |
| Retiring the hand-written migration functions before the Alembic baseline is fully verified | v4.0 | Explicit two-step plan (§7): functions stay inert until baseline-vs-live schema equivalence is proven, not removed on faith. |
| Team unfamiliarity with the new module boundaries slows delivery during the transition | v4.0–v4.2 | Boundary changes are directory/import reorganizations of existing files, not logic rewrites — the audit's file-by-file matrix maps directly onto the new structure. |

---

## 14. What Stays Exactly the Same

- `multi_rpc_manager.py`'s internal algorithm — queue prioritization, adaptive backoff, circuit breaker health scoring, request dedup, TTL cache logic. Only its deployment boundary changes (§5, §6).
- `wallet_crypto.py`'s envelope encryption scheme (AES-256-GCM, per-wallet data key) and its `MasterKeyProvider` abstraction — this is the exact seam v4's KMS work plugs into (§8).
- `admin_rbac.py`'s role hierarchy, owner-immutability invariants, and activity log.
- `real_trade_engine.py` + `jupiter_swap.py`'s Jupiter quote → build → sign (`solders`) → send → confirm flow — confirmed real and correctly built in the audit.
- `conviction_scorer.py`'s scoring formulas and weightings — may be tuned over time, not discarded; the audit confirmed this is real, deliberate heuristic logic with a documented history of past fixes, not throwaway code.
- All existing model column definitions — v4 adds foreign keys and indexes, it does not redesign the schema's shape.
- The Paper Trading simulation logic in `paper_engine.py`.

## 15. What Must Be Rewritten Completely

- **`main.py`'s bootstrapping** — the sequential single-process startup sequence and the 10-loop `asyncio.create_task` soup are replaced by the multi-service composition in §3/§11. This is structural, not a logic rewrite of any individual loop's business rules.
- **FSM storage backend** — mechanically a one-line swap (`MemoryStorage` → `RedisStorage`) but classified as a full replacement because it changes a load-bearing infrastructure assumption every handler implicitly relies on.
- **The hand-written schema migration functions** — fully replaced by Alembic revisions (§7); this is the one piece of v3 the audit found to have already caused a real incident, and it is not preserved in any form once the Alembic baseline is verified.
- **The market-data provider transport layer** (`dexscreener.py`, `geckoterminal.py`, `coingecko.py`, `goplus.py`'s HTTP/session/retry handling) — field-mapping logic is preserved, but the "new `ClientSession` per call, no cache, no retry" transport pattern is fully replaced by the Provider Gateway (§6).
- **`bot/handlers/`** — confirmed dead code in the audit; deleted outright, not migrated.
- **The per-handler permission-check pattern** — replaced by middleware (§8); the *policy* (who can do what) is unchanged, only *where* it's enforced.
- **`bot/commands/real_wallet.py` and `paper_trading.py`** — split into feature-scoped files (§4); handler logic inside each is preserved, the file organization is not.
- **KOL tracking's provider dependency model** — cannot be "preserved as-is" because the audit confirmed it does nothing today; this requires an explicit product decision (ship a real provider, or gate the UI honestly) rather than being carried forward silently.

---

## 16. Estimated Engineering Complexity per Subsystem

| Subsystem | Complexity | Notes |
|---|---|---|
| Dockerize + CI/CD scaffolding | **S** | No existing container definition to reconcile against; mostly new, low-risk since it doesn't touch app logic. |
| Alembic baseline + FK phased rollout | **L** | Baseline is moderate; FK enforcement is gated by real-data orphan discovery, which is inherently unpredictable in size. |
| Redis-backed FSM swap | **S** | Native aiogram feature; mechanical change, thoroughly tested library code. |
| Provider Gateway Service extraction | **M** | Logic is preserved verbatim (non-negotiable); the work is entirely in the service boundary and internal transport, not the algorithm. |
| Market-data providers folded into Provider Gateway | **M** | Four adapters, each needs its field-mapping logic preserved but transport rewritten — repetitive rather than deeply hard. |
| Bot Gateway webhook conversion + horizontal scale-out | **M** | aiogram supports webhook mode natively; the complexity is in load-balancer/TLS/infra setup, not application code. |
| Split Signal/Trading Worker vs. Intelligence Worker | **M** | Mostly a deployment/packaging change; the Premium engine's isolation was already half-built via its feature flag. |
| Distributed locking for background jobs | **S–M** | Well-trodden pattern (Redis lock/leader election); risk is in getting lock-timeout tuning right, not the concept. |
| KMS integration | **M** | The abstraction boundary already exists (`MasterKeyProvider`); complexity is in key re-wrapping migration safety, not new cryptography. |
| Live-validating the three self-flagged risk modules | **M** | Not a code complexity problem — it's a "confirm real API behavior against real data" problem, which takes calendar time more than engineering effort. |
| Observability stack (Sentry, Prometheus, OTel, structured logs) | **M** | Standard integrations; the useful instrumentation (`provider_stats`) already exists internally and just needs exposing. |
| God-file split (`real_wallet.py`, `paper_trading.py`, `pump_radar.py`) | **L** | High line count, high handler density, and — for `real_wallet.py` specifically — real-money blast radius demands careful, well-tested extraction rather than a fast mechanical split. |
| Test suite bootstrap | **L** | Starting from zero (audit confirmed no tests exist anywhere); prioritized rollout (§12) rather than one big effort. |
| Structural permission-check middleware | **S** | Policy is unchanged, only its enforcement location moves. |

*(S = single-sprint scale, M = multi-sprint, L = spans multiple v4.x phases)*

---

**This document is the authoritative reference for AlphaPulse v4.** Any implementation work should cite the relevant section number here, and any deviation from it — preserving something marked "rewrite," or rewriting something marked "stays the same" — should be treated as an explicit, discussed exception, not a silent drift.
