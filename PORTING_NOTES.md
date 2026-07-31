# Porting Notes

This document records everything the porting brief asked me to call out
explicitly rather than silently deciding: behavior that couldn't be ported
1:1 and why, library-driven differences, everything added beyond a literal
port, and judgment calls made on ambiguous points. It also gives an honest
account of what I actually verified before handing this back, and what I
could not run in this environment.

## 1. Verification: what I actually ran, and what I could not

I want to be direct about the limits of what I could test, per your item #3
("confirmation that you ran the test suite, linter, and type checker
yourself before handing it back, with the output"):

**This sandbox has no network access**, so `pip install aiogram solders
solana sqlalchemy alembic redis arq pydantic-settings structlog httpx pytest
ruff mypy ...` all fail with "no matching distribution found." Only
`cryptography` was pre-installed. I could not install the project's real
dependencies, so I could not run the *actual* `pytest`, `ruff`, or `mypy`
against the full project, and I'm not going to claim otherwise.

What I *did* do instead, since I could not just take the port on faith:

- **Manually executed every ported unit test** (`tests/unit/test_scoring.py`,
  `test_risk_engine.py`, `test_signal_engine.py`) with a small hand-rolled
  runner (no `pytest` available), since `scoring.py`, `risk.py`, and
  `signal.py` have zero third-party dependencies (stdlib + `dataclasses`
  only). All 15 ported test cases passed.
- **Cross-validated the scoring and signal engines numerically against the
  original JS logic directly**, using Node (which *is* available in this
  sandbox) as a reference oracle rather than trusting my own translation:
  - Re-implemented `scoreWallet` and `evaluateTokenCluster` verbatim in a
    throwaway `.mjs` script, generated 20 and 15 randomized fuzz inputs
    respectively, and diffed the JS output against the Python port's output
    for the *same* inputs. All 20 scoring cases and all 15 signal-clustering
    cases matched exactly (score, confidence, flags, risk level, wallet
    count, contributing wallets).
  - This surfaced one real discrepancy I fixed: **JavaScript's `Math.round`
    rounds half-values up (toward +∞), while Python's built-in `round()` uses
    banker's rounding (round-half-to-even)**. I added `engines/_js_compat.js_round`
    and used it everywhere the original called `Math.round` on a score/confidence
    value, so tie-breaking behavior matches exactly. Without this fix, the fuzz
    cross-check did occasionally disagree on exact `.5` boundaries.
- **Could not run**: `mypy --strict` against the full tree, `ruff check`,
  `ruff format --check`, the `aiogram`/`sqlalchemy`/`solders`-dependent code
  paths (bot handlers, DB models, trade executor, reconciliation), or the
  Alembic migration against a live Postgres instance. I wrote all of this
  code carefully and consistently with the patterns in the parts I *could*
  verify, but **you should run `pip install -e ".[dev]"`, `ruff check .`,
  `mypy src`, `alembic upgrade head` against a throwaway Postgres, and
  `pytest -v` yourself before trusting this in anything beyond local dev** —
  the CI workflow I wrote (`.github/workflows/ci.yml`) will do exactly this
  on every push/PR once you push the repo, which is the fastest way to get a
  real, environment-verified answer.

## 2. Judgment calls (things I decided rather than leaving ambiguous)

- **Prisma `cuid()` primary keys**: there's no canonical Python cuid2
  implementation in the stdlib or a dependency I could verify installs
  cleanly offline. `db/models.py::generate_id()` produces a 25-character
  lowercase-alphanumeric id using `secrets.choice`, satisfying the practical
  properties Prisma's `cuid()` gave the original (unique, URL-safe,
  non-guessable primary keys) without claiming byte-for-byte cuid
  compatibility. If you need actual interop with cuid-format ids from an
  existing TS deployment's data, swap in a proper `cuid2`-compatible library
  instead.
- **`Trade.reconciliation_attempts` / `last_blockhash` / `last_valid_block_height`
  / `submitted_at` columns**: added to the `Trade` model (not present in
  `schema.prisma`) specifically to support restart-safe reconciliation (see
  §3). These are pure additions — no existing column was renamed or removed,
  so a straight data migration from the original Postgres schema only needs
  to `ALTER TABLE trades ADD COLUMN ...` for these four columns with
  sensible defaults, not a full re-migration.
- **`trading.py` `/portfolio` and `/autotrading` commands**: the original TS
  `trading.ts` only implements *read* commands (`/portfolio`, `/autotrading`)
  — there's no `/buy`, `/sell`, or `/autotrading_setup` command wired up in
  the TS source I was given, only text referencing them. I ported exactly
  what exists (the two read commands) and left the referenced-but-unimplemented
  commands unimplemented, exactly matching the original's actual behavior
  rather than inventing a plausible-looking `/autotrading_setup` handler that
  wasn't in the source.
- **USD -> lamports conversion placeholder**: the original
  `autoTradingEngine.ts` has `Math.round((check.proposedTradeUsd / 1) * 1e9)`
  with a `TODO(integration)` comment admitting the `/ 1` is a placeholder
  pending a live SOL/USD price feed. I ported this exactly, including the
  placeholder division by 1, rather than "fixing" it — per requirement #1,
  changing trading-relevant math isn't mine to decide silently. Do not
  deploy this against real funds until that TODO is resolved with a real
  price feed; right now every "USD" trade size is silently treated as if
  1 USD = 1 SOL, which is wrong and will send wildly incorrect trade sizes.
- **aiogram vs python-telegram-bot**: I chose aiogram v3 over PTB v21, since
  aiogram's `Router` + typed-filter model maps more directly onto grammY's
  `bot.command(...)` + middleware chain than PTB's `ConversationHandler`-
  oriented API does, keeping the command bodies closer to 1:1 with the
  original.
- **RBAC dispatch shape**: grammY's `requireAdmin` is a composable middleware
  passed as a second argument to `bot.command(...)`. aiogram v3's typed-kwarg
  injection (handlers receive `is_admin` as a parameter populated from
  middleware `data`) doesn't compose quite the same way per-command, so
  `bot/commands/admin.py` handlers each check `is_admin` at the top of the
  function body instead of via a per-command filter. Net behavior is
  identical (bot-layer gate, independently re-checked in
  `WhaleWalletAdminService`); only the wiring mechanism differs.

## 3. New functionality: restart-safe trade reconciliation

Per explicit instruction, this is **new**, not in the original TS code:

- `Trade` model gained `last_blockhash`, `last_valid_block_height`,
  `submitted_at`, `reconciliation_attempts` columns.
- `engines/trade_executor.py::execute_trade` now writes the pre-existing
  `PENDING` Trade row to `SUBMITTED` (recording the blockhash used) **before**
  calling `send_raw_transaction`, and updates it to `CONFIRMED`/`FAILED`
  after `confirm_transaction` resolves — so there is always a durable record
  between "decided to trade" and "transaction resolved."
- `engines/auto_trading.py::process_signal_for_auto_trading` creates that
  `PENDING` row before calling the executor at all (covering the case where
  the process crashes before ever reaching Jupiter).
- `engines/reconciliation.py::reconcile_pending_trades(session, connection)`
  is a new module, called once at startup in `main.py` before the bot starts
  polling or the scheduler starts running. It:
  - Marks `PENDING` trades with no `tx_signature` and no `submitted_at` as
    `CANCELLED` (nothing was ever broadcast).
  - For `SUBMITTED` trades with a signature, queries
    `get_signature_statuses` — confirmed-with-no-error -> `CONFIRMED`;
    confirmed-with-error -> `FAILED`; not found and the recorded blockhash's
    `last_valid_block_height` has already passed -> `FAILED` (the transaction
    can no longer land, full stop); not found but still within its valid
    window -> left as `SUBMITTED` for the next pass.
  - Caps retries at `MAX_RECONCILIATION_ATTEMPTS` (10) to avoid an infinite
    loop on a persistently-unreachable RPC, after which it marks `FAILED` and
    writes an `AuditLog` row flagging the trade for manual review.
- I did **not** invent a value for what happens to the user-facing risk-state
  counters (`open_positions`, `trades_today`, `exposure_usd_today`) once a
  stuck trade resolves to `FAILED` post-reconciliation — the original TS
  code has no equivalent state-recomputation step at all (those counters
  aren't shown being persisted/decremented anywhere in the source I was
  given), so I left that as an open integration point rather than guessing
  at a reconciliation-adjacent feature that wasn't part of either the
  original or the explicit requirement. Flag this before going live: a
  reconciled `FAILED` auto-trade should very likely free up the user's daily
  trade count / exposure budget it consumed, and that bookkeeping isn't
  wired up yet in either codebase.

## 4. Library-driven differences

| Concern | TypeScript original | Python port | Note |
|---|---|---|---|
| Telegram bot framework | grammY | aiogram v3 | See §2 for why. |
| ORM | Prisma (`schema.prisma`) | SQLAlchemy 2.0 async + Alembic | Schema semantics preserved exactly; see §2 for the id-generation caveat. |
| Env validation | Zod | pydantic v2 + pydantic-settings | Same fields, same defaults, same validation rules (regex, url, enum, coercion). |
| Logging | pino (+ pino-pretty in dev) | structlog (ConsoleRenderer in dev, JSONRenderer otherwise) | Same redaction behavior, keyed on the same conceptual field names. |
| Queue / scheduler | BullMQ repeatable job (planned) / `setInterval` (actual) | `asyncio.create_task` + sleep loop | The TS code itself only used `setInterval`, not BullMQ, despite BullMQ being in the stack list — I matched the *actual* TS behavior (simple interval), not the aspirational one. |
| Solana SDK | `@solana/web3.js` | `solders` + `solana-py` | `Keypair.from_bytes`/`VersionedTransaction.from_bytes` map onto `Keypair.fromSecretKey`/`VersionedTransaction.deserialize`. |
| HTTP client (Jupiter) | native `fetch` | `httpx.AsyncClient` | Same request/response shapes preserved. |
| Async model | Node's single-threaded event loop, implicit `await` everywhere | Python `asyncio`, same `await`-everywhere style | No behavioral difference expected; both are cooperative single-threaded event loops. The one place this matters is the in-memory `EventBuffer` (`engines/monitor.py`), which I additionally wrapped in a `threading.Lock` even though aiogram/asyncio code is single-threaded by default — cheap insurance if this ever gets called from a worker thread (e.g. a sync Alembic hook), with no behavioral cost in the common case. |

## 5. Things I did not weaken

Per the hard requirements:

1. **Scoring/risk/signal logic**: ported line-for-line, weight-for-weight,
   threshold-for-threshold, then numerically cross-validated against the
   real JS logic (see §1). The only behavioral change is the `js_round` fix
   described above, which makes Python's output **more** faithful to the
   original's actual rounding behavior, not less.
2. **Key handling**: keys are decrypted only for the duration of
   `execute_trade`, into a `bytearray` that's explicitly zeroed in a `finally`
   block; never logged (the redaction list covers the relevant field name
   patterns). See §1 of `docs/SECURITY.md` for the honest caveat about
   CPython's zeroing guarantees vs. Node's.
3. **Restart-safe execution**: implemented as described in §3, clearly new.
4. **Config validation fails loudly**: `config.py::load_env()` catches
   `pydantic.ValidationError`, prints a clear message, and raises — the
   process refuses to start on bad/missing env vars, same as the original's
   `loadEnv()`.
5. **RBAC/defense-in-depth**: `WhaleWalletAdminService._assert_admin` re-checks
   the actor's role independently of the bot-layer `is_admin` gate in every
   admin command handler, exactly mirroring the original's two-layer check.
