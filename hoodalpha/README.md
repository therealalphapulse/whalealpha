# HoodAlpha

A completely separate Telegram bot/service that tracks meaningful
price dips on tokens WhaleAlpha has already alerted, and signals when
price recovers 20%+ (configurable) off the lowest confirmed dip.

**Shares nothing at runtime with WhaleAlpha** except the same Postgres
instance (own schema, `hoodalpha`) and the same Redis instance (own,
disjoint lock-key prefix). See `hoodalpha/__init__.py` for the full
independence rationale. Nothing in WhaleAlpha's existing code was
changed to add this package — `models/__init__.py`, `config/settings.py`,
`main.py`, the `Procfile`, and `docker-compose.yml` are all untouched.

## Env vars (new, additive only)

| Var | Required | Default | Notes |
|---|---|---|---|
| `HOOD_BOT_TOKEN` | yes | — | A **new** BotFather bot token. Never reuse WhaleAlpha's `BOT_TOKEN`. |
| `HOOD_TELEGRAM_CHAT_IDS` | yes (for alerts) | empty | Comma-separated chat IDs and/or `@channel` usernames. |
| `DATABASE_URL` | yes | — | Point at the **same** Postgres WhaleAlpha uses (e.g. a Railway `${{Postgres.DATABASE_URL}}` reference variable). |
| `REDIS_URL` | recommended | empty | Same Redis instance WhaleAlpha uses. Falls back to an in-process lock if unset — **not safe** with >1 replica. |
| `HOOD_MIN_DIP_PCT` | no | `10.0` | Minimum % below WhaleAlpha's entry price before a dip is "confirmed" and the rise check arms. |
| `HOOD_RISE_THRESHOLD_PCT` | no | `20.0` | % rise off the lowest dip that fires a HoodAlpha signal. |
| `HOOD_INGEST_INTERVAL_SECONDS` | no | `120` | How often `hoodalpha.watch` is seeded from new WhaleAlpha alerts. |
| `HOOD_MONITOR_INTERVAL_SECONDS` | no | `90` | How often watched tokens are re-priced. |
| `HOOD_INGEST_BATCH_SIZE` | no | `500` | Max signal_tokens rows pulled per ingest cycle. |
| `HOOD_STALE_AFTER_HOURS` | no | `168` | Hours with no usable price before a watch is marked stale and stops being polled. |
| `HOOD_FALLBACK_DEXSCREENER_ENABLED` | no | `true` | Whether to call DexScreener directly when a token has fallen out of WhaleAlpha's active signal set. |

## Deploy order (Railway)

1. **Merge this code to the branch Railway will build from.**
2. **Run the bootstrap once** (creates the `hoodalpha` schema/tables —
   idempotent, safe to re-run every deploy, same role as WhaleAlpha's
   own `release: python -m app_platform.gateway.bootstrap`):
   ```
   python -m hoodalpha.bootstrap
   ```
   Either as a one-off Railway command, or add it as that service's
   **Pre-Deploy Command** in Railway's service settings.
3. **Create two new Railway services** (same project as WhaleAlpha, or
   a new project — either way, point `DATABASE_URL`/`REDIS_URL` at
   WhaleAlpha's existing Postgres/Redis via reference variables):

   | Service | Start command |
   |---|---|
   | `hoodalpha-worker` | `python -m hoodalpha.worker` |
   | `hoodalpha-bot` | `python -m hoodalpha.gateway` |

4. Set the env vars above on **both** new services.
5. Deploy. `hoodalpha-worker`'s ingest loop will begin picking up
   WhaleAlpha signals whose `alert_delivered = true` on its very first
   cycle (there is no backfill cutoff — the ingest cursor starts at 0).

No changes to WhaleAlpha's three existing services are required or
made.

## Rate limits / load

- **DexScreener**: the primary price path piggybacks on WhaleAlpha's
  own `signal_lifecycle_loop` (it already polls every active signal
  every ~90s) via a read-only SQL query — **no additional DexScreener
  calls** for tokens WhaleAlpha is still actively tracking. HoodAlpha's
  own `hoodalpha/marketdata.py` DexScreener client only fires as a
  fallback for tokens WhaleAlpha has stopped tracking, and is
  independently rate-limited/cached (20s in-memory cache per contract)
  from WhaleAlpha's own request cache.
- **Postgres**: read-only queries against `public.signal_tokens` are
  simple indexed lookups; writes are confined to `hoodalpha.*` with
  their own indexes. Watch/monitor cycle cost scales with the number of
  actively-watched tokens, not with WhaleAlpha's total signal history.
- **Telegram**: HoodAlpha uses its own bot, so its message volume never
  counts against WhaleAlpha's bot's rate limits (Telegram rate-limits
  per bot token).

## Known limitations / things worth revisiting

- `hoodalpha/db.py::fetch_current_prices` matches WhaleAlpha's
  `signal_tokens` by `contract` alone (asyncpg has no clean composite
  `(contract, chain) IN (...)` binding), then filters by `chain` in
  Python. This is correct today but revisit if WhaleAlpha's discovery
  ever produces a genuine `(contract, chain)` collision — see the
  comment in that function.
- Not yet exercised against a live Postgres/Redis in this environment
  (sandboxed, no route to Railway's private network) — only
  syntax-checked and import-checked. Recommend a staging smoke test
  (`hoodalpha.bootstrap` against a scratch database, then a few manual
  `ingest_once()` / `run_cycle()` calls) before the first production
  deploy.
