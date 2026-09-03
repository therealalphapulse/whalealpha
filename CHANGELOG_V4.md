# AlphaPulse v3 → v4 Migration: Change Summary

This document is the required deliverable summarizing what changed, what
was added, what was removed, and — critically — what has **not** been
verified against live infrastructure and still needs that verification
before production use. It is written to be read on its own, without
needing to cross-reference every commit.

## Environment disclosure (read this first)

This migration was performed in a sandboxed environment with **no
network access**, **no Docker daemon**, **no live Postgres**, **no live
Redis**, and only one of AlphaPulse's own dependencies installed
(`cryptography`). Every claim below is scoped honestly by what could
actually be verified here:

- **Syntax-verified**: the entire tree compiles (`py_compile`) after
  every change — checked continuously throughout, not just at the end.
- **Behaviorally-verified (real, running tests)**: `infra/kms/` (wallet
  encryption round-trip, 6/6 tests) and `infra/locks.py` (distributed
  locking / leader election, 6/6 tests, including a genuine crash-
  recovery scenario) — see `tests/`.
- **Structurally-verified**: the import-boundary linter
  (`scripts/check_import_boundaries.py`) was run against the real,
  restructured tree (zero violations) and proven to actually catch
  violations (a scratch violation was temporarily introduced and
  correctly flagged, then removed).
- **Written but NOT runtime-verified**: anything requiring aiogram,
  SQLAlchemy, asyncpg, Redis, a live Postgres, a live Solana RPC, Docker,
  or AWS — i.e., most of the actual bot-serving and trading code paths.
  This includes the Dockerfile, docker-compose.yml, the webhook
  entrypoint, the Alembic migrations, and every moved service module's
  *runtime* behavior (their *syntax* and *import graph* are verified;
  their behavior when actually run against live infra is not, because
  none was available). **Run this through a real CI pipeline and a
  staging deploy before trusting it with production traffic or real
  funds.**

## Major architectural changes

| Area | v3 | v4 |
|---|---|---|
| Directory structure | `bot/`, `services/`, `models/`, `config/` (flat, mixed responsibilities) | `app_platform/`, `domain/`, `providers/`, `infra/`, `workers/`, `models/` — bounded contexts per the Architecture Bible §4 |
| Telegram update delivery | Long-polling, single process only (hard Telegram API constraint) | Webhook mode available (`app_platform/gateway/webhook_entrypoint.py`) for horizontal scaling; polling mode kept for dev/single-instance |
| FSM (conversation) state | aiogram default `MemoryStorage`, in-process only | Redis-backed when `REDIS_URL` is set, in-memory fallback otherwise (`app_platform/gateway/app.py`) |
| Background loops | ~10 `asyncio.create_task` loops sharing one event loop with user commands in `main.py` | Split into `workers/signal_trading_worker.py` and `workers/intelligence_worker.py`, separate deployable processes, each loop under real distributed leader election (`infra/locks.py`) |
| Provider layer | Two disconnected tiers: RPC (resilient) vs. market-data (no cache, no retry, session-per-call) | Both tiers share the same resilience discipline — market-data providers now route through `providers/marketdata/_resilience.py` (shared cache, retry, timeout, session reuse) |
| Wallet key custody | `EnvMasterKeyProvider` only | `MasterKeyProvider` interface unchanged; added `KMSMasterKeyProvider` (AWS KMS) as a second, production-grade implementation — zero changes to `encrypt_secret`/`decrypt_secret` call sites |
| Permission enforcement | Per-handler `has_permission()`/`is_premium()` calls | Additive structural middleware (`RBACMiddleware`, `PremiumMiddleware`) via aiogram flags; existing per-handler checks untouched and still work |
| Schema migrations | Hand-written `migrate_*_schema()` functions run on every boot | Alembic scaffolding added (`infra/db/migrations/`); hand-written functions **kept running** until a live-database autogenerate pass verifies the Alembic baseline (see Gaps below) |
| Foreign key integrity | ~25 tables with `user_id`-shaped columns, no FK constraint | `ForeignKey("users.telegram_id")` added to all 25 in `models/*.py`; phased `NOT VALID` → `VALIDATE` Alembic migrations written (`0002`, `0003`) |
| Observability | `logging.basicConfig` to stdout only, no metrics, no tracing, no error tracking | Structured JSON logging with correlation IDs, Prometheus metrics (surfacing `multi_rpc_manager`'s existing internal stats), optional Sentry integration — all in `infra/observability/` |
| Deployment | Railway-only (Nixpacks buildpack, no Dockerfile) | `Dockerfile` + `docker-compose.yml` implementing the full multi-service topology; deployment-agnostic |
| CI | None | `.github/workflows/ci.yml` — syntax check, import-boundary check, dependency-light test suite, Docker build |

## Files/modules added (new in v4)

- `providers/protocol.py`, `providers/cache.py`, `providers/marketdata/_resilience.py`
- `infra/kms/provider.py`, `infra/kms/env_provider.py`, `infra/kms/kms_provider.py`
- `infra/locks.py`, `infra/observability/logging_config.py`, `infra/observability/metrics.py`, `infra/observability/error_tracking.py`
- `infra/db/alembic.ini`, `infra/db/migrations/` (env.py + 3 revisions + README)
- `app_platform/gateway/app.py`, `bootstrap.py`, `polling_entrypoint.py`, `webhook_entrypoint.py`
- `app_platform/middleware/` (4 middleware modules)
- `domain/signals/keyboard_provider.py`
- `domain/admin/user_service.py` (moved from `services/database.py`, renamed for clarity)
- `workers/signal_trading_worker.py`, `workers/intelligence_worker.py`
- `models/__init__.py` (see Gaps — this didn't exist in v3 at all)
- `scripts/check_import_boundaries.py`
- `tests/test_wallet_crypto.py`, `tests/test_locks.py`, `tests/README.md`
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.github/workflows/ci.yml`

## Files/modules removed

- `bot/handlers/` — confirmed dead code in the audit (empty, unreferenced
  router); deleted outright, not migrated, per the Bible §15.

## Files/modules moved (logic unchanged unless noted)

All of `services/` and `bot/` were relocated into their v4 domain homes
— see the Module Boundary Redesign Plan (Bible §4) for the full mapping.
Two files had a small, targeted logic change during the move:

- `domain/signals/pump_radar.py` and `domain/signals/alert_engine.py` —
  the confirmed layering violation (`import app_platform.keyboards`
  directly) was fixed via dependency injection
  (`domain/signals/keyboard_provider.py`). Everything else in both files
  is unchanged.
- `infra/kms/wallet_crypto.py` — `MasterKeyProvider`/`EnvMasterKeyProvider`
  were split out into their own files (`infra/kms/provider.py`,
  `env_provider.py`); the AESGCM encrypt/decrypt logic itself is
  byte-for-byte unchanged and test-verified identical behavior.
- `domain/intelligence/risk_engine.py` — `LP_LOCK_REJECT_BELOW` now reads
  from `config.settings.SIGNAL_MIN_LOCKED_LIQUIDITY_PCT` instead of being
  a disconnected hardcoded duplicate (fixes the audit's "dead
  configuration" finding — verified with a live test that changing the
  env var now actually changes enforcement).

`bot/commands/real_wallet.py` and `paper_trading.py` were **moved but
deliberately NOT split** into smaller files, despite the Bible's module
plan showing them decomposed — see Deliberate scope decisions below.

## Deliberate scope decisions (and why)

1. **`real_wallet.py` (1,909 lines) and `paper_trading.py` were not
   split.** The Bible's own risk assessment (§13) and the task's
   instruction to "keep the project stable and fully functional
   throughout" both argue against machine-splitting real-money handler
   code with zero test coverage to catch a broken import or a dropped
   handler. They were moved intact into `app_platform/commands/` with
   zero behavior change. Splitting them is tracked as a v4.1 follow-up,
   to be done only once test coverage exists for them specifically.

2. **The hand-written `migrate_*_schema()` functions were not deleted.**
   Per the Bible's explicit two-step plan (§7), they remain in place and
   still run at bootstrap until a real `alembic revision --autogenerate`
   pass has been run against a live copy of the production database and
   verified to match the `0001_baseline` migration's assumption (that the
   schema already matches `models/*.py`). That verification requires a
   live Postgres this environment did not have.

3. **Structural RBAC/Premium middleware is additive, not a replacement.**
   Existing per-handler `has_permission()`/`is_premium()` checks in
   `admin_panel.py`, `premium.py`, etc. were **not** removed or rewritten.
   The new middleware only activates for handlers that opt in via an
   aiogram flag. Migrating existing handlers to the flag-based pattern is
   incremental, tracked as v4.1 follow-up work, not a blind mass edit of
   already-working, unreviewed-by-tests admin/premium code.

## Known gaps and what to do before production use

1. **Run the real CI pipeline.** `.github/workflows/ci.yml` has not
   actually executed anywhere — it was written to the documented GitHub
   Actions syntax but this environment cannot run it. Push this branch
   and confirm it goes green before merging.

2. **Validate the Alembic baseline against a live database**
   (`infra/db/migrations/README.md` has the exact steps) before retiring
   the hand-written migration functions.

3. **Verify the FK migration's table names against the real, deployed
   schema** before running `0002`/`0003` — they were derived directly
   from `models/*.py`'s `__tablename__` values (not guessed), but that
   is "matches the source code," not "matches what's actually deployed."
   Run the orphan-check query in the migrations README regardless.

4. **`docker build` / `docker compose up` have not been run.** The
   Dockerfile and docker-compose.yml follow standard, well-documented
   patterns but were authored with no Docker daemon available to test
   against. Build and run them in a real environment before deploying.

5. **The webhook entrypoint has not been exercised against a live
   Telegram webhook delivery** — no public HTTPS endpoint or network
   access was available here. Same caveat applies as the audit already
   applied to other network-dependent, self-flagged-unverified modules
   (`lp_lock_checker.py`, `funding_graph.py`, etc.) — treat it with the
   same staging-first discipline.

6. **`KMSMasterKeyProvider` (AWS KMS backend) has not been exercised
   against a real KMS endpoint** — no AWS credentials or network access
   available here. Test it in staging against a real (non-production)
   CMK before migrating any real wallet's master key to it.

7. **Test coverage is intentionally narrow** — see `tests/README.md` for
   the full, honest breakdown of what is and isn't covered, and the
   recommended next additions.

8. **Two pre-existing product/data gaps the audit found are unchanged by
   this migration**, because fixing them is a product decision, not an
   architecture one: KOL wallet tracking still does nothing without an
   operator-supplied `KOL_PROVIDER_URL`, and there is still no RugCheck
   integration anywhere in the codebase.

## A mistake made and caught during this migration

An early scripting error in the bulk file-move step accidentally deleted
`config/settings.py` (a variable-naming bug caused a placeholder path to
be written over the real file, which was then compounded by deleting
what was assumed to be a leftover placeholder). This was caught by the
next compile-check step, diagnosed, and fixed by restoring the file from
the untouched original source copy — verified afterward with a full
tree recompile. Documented here rather than silently, per the same
standard the rest of this migration was held to.
