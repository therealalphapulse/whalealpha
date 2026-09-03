# AlphaPulse v4 — Test Suite

Part of the v4.0 "Foundation" phase (Bible §12). The audit found **zero**
automated tests anywhere in v3. This is the start of a real suite, not a
complete one — scope below is stated honestly rather than implied to be
more than it is.

## What's covered, and actually passing

Both suites below were written and *run* (not just written) during the
v4 migration, in an environment with no network access, no Docker, no
Postgres, and no Redis available — they were deliberately scoped to only
require `cryptography` (stdlib + one package) so they can run anywhere,
including CI runners and this sandbox:

- **`test_wallet_crypto.py`** (6 tests) — encryption/decryption round-trip,
  fresh-nonce-per-call, wrong-master-key failure, and the
  `MasterKeyProvider`/`EnvMasterKeyProvider` split introduced in v4
  (`infra/kms/`). Targets the highest-value module to cover first: it
  protects every custodial private key in the system.
- **`test_locks.py`** (6 tests) — the distributed lock and leader-election
  primitives (`infra/locks.py`) that `workers/` relies on to guarantee a
  real-money trading loop never runs twice across multiple replicas.
  Includes a genuine crash-recovery scenario (a wrapped loop raising an
  exception mid-run), not just the happy path.

Run either directly:
```bash
python tests/test_wallet_crypto.py
python tests/test_locks.py
```

## What's explicitly NOT covered yet

Everything that requires a live Postgres, Redis, Solana RPC, or Telegram
API connection — none of which were available while this migration was
authored:

- `domain/trading/real/*` (Jupiter quote/sign/send/confirm flow) — the
  audit confirmed this logic is real and correct by reading it, but that
  is not the same as a test exercising it against a live (or even a
  mocked) RPC/Jupiter response.
- `domain/admin/admin_rbac.py` — needs a real or mocked DB session.
- `domain/signals/scoring.py` (conviction scoring) and `risk_engine.py` —
  pure-logic-testable in principle (no I/O in the scoring math itself)
  but not yet covered; a natural next addition since it doesn't need live
  infra either.
- Any aiogram handler (`app_platform/commands/*`) — needs aiogram
  installed (not available in the authoring sandbox) plus mocked
  `Message`/`CallbackQuery` objects.
- `providers/marketdata/*` and `providers/rpc/*` — needs either live
  network access or a mocked `aiohttp` session.

## Recommended next additions (v4.1, per the Bible's roadmap)

1. `domain/signals/scoring.py` + `risk_engine.py` — pure logic, no new
   infra needed, highest test-per-effort ratio available.
2. `app_platform/middleware/*` — mock aiogram's `Message`/`data` dict,
   verify `RBACMiddleware`/`PremiumMiddleware` actually block when they
   should.
3. A `docker-compose.test.yml` (Postgres + Redis, ephemeral) enabling
   real integration tests for `domain/admin`, `domain/trading`, and the
   Alembic migrations themselves — none of this was possible to build
   and verify in the current sandbox.
