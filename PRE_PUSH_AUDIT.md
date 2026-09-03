# AlphaPulse v4 — Pre-Push Audit

Run against the restructured repository before pushing to GitHub. This is
a real audit, not a re-assertion of the migration changelog — every check
below was actually executed against the tree, and every finding is
reported whether or not it looked good.

## What could and couldn't be checked here

Same environment constraints as the migration itself: no network, no
Docker daemon, no live Postgres/Redis, and only `cryptography` installed
from the actual dependency list. That means **static analysis** (does
every import resolve, is there a circular import, does every call site
match its target's real signature) could be done exhaustively and
automatically. **Runtime behavior** (does aiogram's middleware chain
actually fire in the order expected, does the webhook handler actually
serve a request, does Alembic actually apply a migration) could not —
those still need a real CI run and a staging deploy, exactly as flagged
in `CHANGELOG_V4.md`.

## Checks performed

### 1. Full-tree syntax check
`python -m py_compile` against every `.py` file in the repository.
**Result: clean, 0 errors.**

### 2. Import boundary enforcement
`scripts/check_import_boundaries.py`, plus a live negative test (a
temporary violation was deliberately introduced and confirmed caught,
then removed).
**Result: 0 violations in the real tree; linter proven to actually catch
violations, not just trivially pass.**

### 3. Internal import resolution (432 statements checked)
A purpose-built checker (not reused from anywhere) parsed every internal
`from X import Y` in the repository and verified `Y` is genuinely defined
in `X` — catching the class of bug `py_compile` cannot (importing a name
that doesn't exist, a typo'd function name, etc.). First pass found 4
apparent issues; on investigation 1 was a valid submodule import pattern
and 3 were names defined inside `try/except` or `if/else` blocks at
module level (still valid, just missed by the checker's initial
scope-detection logic). The checker was corrected to handle both cases
properly and re-run.
**Result after correction: 0 unresolved internal imports.**

### 4. Circular import detection
A module-level dependency graph was built via AST parsing and searched
for cycles (DFS with cycle detection). 42 candidate cycles were flagged;
40 were the checker picking up deferred imports *inside function bodies*
(intentionally lazy, not module-level) as if they were top-level edges —
`infra/db/session.py`'s `import models` inside `init_db()`, and
`infra/kms/provider.py`'s provider-selection imports inside
`get_key_provider()`. The remaining 2 (`pump_radar.py`↔`signal_tracker.py`
and `premium_service.py`↔`premium_signal_engine.py`) were checked by hand:
in both cases one direction imports at module level and the other
direction imports lazily inside a function — a standard, safe way to
break a cycle, and a pattern that pre-dates this migration (unchanged
from v3, not introduced by the restructuring).
**Result: 0 actual circular imports.**

### 5. Cross-check of every worker/bootstrap call site against real function signatures
Every function called by name in `app_platform/gateway/bootstrap.py`,
`workers/signal_trading_worker.py`, and `workers/intelligence_worker.py`
(10 bootstrap calls + 9 signal/trading loops + 3 intelligence loops) was
looked up directly in its target file and its actual parameter list
compared against the call site.
**Result: all 22 match exactly** (name, and where checked, keyword
argument compatibility) — this was the highest-risk area for a
migration-introduced bug, since these call sites were written from
inference about the original `main.py`'s behavior rather than copied
verbatim, and it checked out.

### 6. Router registration completeness
Verified all 19 command modules registered in
`app_platform/gateway/app.py` actually define a module-level `router`.
**Result: all present.**

### 7. Keyboard dependency-injection seam
Verified `token_actions_keyboard`'s real parameter list matches exactly
what `domain/signals/keyboard_provider.py` passes through it (the fix for
the audit's confirmed layering violation).
**Result: exact match.**

### 8. Middleware call sites vs. real domain function signatures
`AuthMiddleware`, `RBACMiddleware`, `PremiumMiddleware`'s calls into
`get_or_create_user`, `get_role`, `has_permission`, `is_premium`,
`premium_upsell_text` were checked against each function's actual
signature and sync/async nature.
**Result: exact match**, including confirming `premium_upsell_text` is
correctly called without `await` (it's a plain sync function).

### 9. `models/__init__.py` — independent re-verification
Generated in the original migration by extracting every model file's
class names via one script; re-verified here with a **second, differently
written** script parsing `models/__init__.py`'s actual import lines and
cross-checking each imported name against a fresh AST parse of its
source file.
**Result: all 39 imported class names across 37 model files confirmed
correct.**

### 10. Docker/Compose/CI file validity
`docker-compose.yml` and `.github/workflows/ci.yml` parsed with a real
YAML parser (not eyeballed).
**Result: both valid YAML.**

### 11. Environment variable name consistency
Every env var name referenced in `docker-compose.yml` cross-checked
against every `os.getenv(...)` call site (including ones using the
`_env_bool`/`_env_int` helper wrappers, checked separately since they
don't match a simple grep pattern).
**Result: all consistent, no typos found.**

### 12. Alembic path robustness — **1 real issue found and fixed**
`infra/db/alembic.ini`'s `script_location` was a plain relative path
(`infra/db/migrations`), which Alembic resolves relative to the caller's
current working directory, not relative to the `.ini` file's own
location. This meant `alembic -c infra/db/alembic.ini upgrade head` would
only work correctly if invoked from the repo root specifically — anyone
running it from another directory would get a "no such file" error.
**Fixed**: changed to `script_location = %(here)s/migrations`, using
Alembic's location-independent path token (supported since Alembic
1.11, and the pinned version in `requirements.txt` is 1.13.2) — this now
works regardless of the caller's working directory. `env.py`'s own
`sys.path` manipulation was already `__file__`-relative and needed no
change.

### 13. Leftover debug artifacts / scratch files
Searched for `TEMP-DEBUG`, `FIXME`, `XXX`, stray `print()` calls in new
v4 code, and any `.bak`/`*scratch*`/`*_TMP*` files.
**Result:** 2 unrelated `print(` matches in new code were false positives
(one inside a docstring quoting the audit, one inside an example CLI
command shown in an error message string — neither is an executing
`print()` call). No scratch or temp files found. **One pre-existing
finding surfaced, not new**: `app_platform/commands/real_wallet.py` still
contains four `[TEMP-DEBUG]` log lines, carried over unchanged from v3 —
this file was deliberately moved without modification (see
`CHANGELOG_V4.md`'s scope decisions), so this was already flagged in the
original audit and remains open, not silently reintroduced.

## Net result

One real bug found and fixed (the Alembic path issue). Everything else
checked out on first or second inspection — including several apparent
issues that turned out to be false positives from my own checking tools,
which were then corrected rather than either ignored or reported
uncritically. No new circular imports, no broken call sites, no
unresolved references, no invalid config files.

**This audit does not replace running the actual CI pipeline or a
staging deploy** — it closes the gap that static analysis *can* close in
an environment with no live infrastructure, and is explicit about the
gap it can't. See `CHANGELOG_V4.md`'s "Known gaps" section for the
runtime-verification items that still need a real environment.
