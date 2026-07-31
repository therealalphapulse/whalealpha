# Security

## Custody model — read this before enabling real trading

This repo includes a *reference* custodial flow (`User.encrypted_wallet_key`,
`utils/security/encryption.py`, `engines/trade_executor.py`) so the codebase is
complete and runnable end to end. Before it touches real user funds:

1. **Prefer non-custodial.** Have users connect a wallet client-side
   (Phantom/Backpack deep link, or Telegram Mini App + wallet-adapter) and
   sign transactions themselves. The bot builds the unsigned swap
   transaction; the user's own wallet signs it. The server never sees a
   private key.
2. **If you must be custodial**, move key material into a KMS/HSM (AWS KMS,
   GCP KMS, Vault Transit) instead of `ENCRYPTION_KEY` + AES in app memory.
   App-level AES is a reasonable baseline for a hobby project, not for
   holding other people's money.
3. **Get an independent security review** of the signing path specifically
   before accepting real deposits. This is the single highest-consequence
   code path in the repo.
4. **Python-specific key hygiene note**: unlike Node's `Buffer.fill(0)`,
   CPython gives no hard guarantee that a decrypted secret's backing memory
   is zeroed and reclaimed on a fixed schedule (immutable `bytes`/`str` may be
   copied internally; the GC reclaims on its own schedule). This port
   decrypts into a mutable `bytearray` and explicitly overwrites it
   immediately after signing (`zero_bytearray` in
   `utils/security/encryption.py`) — the strongest guarantee available
   without a native extension or a KMS boundary. Treat this as best-effort,
   not a hard guarantee, exactly like the original TS `Buffer.fill(0)` call
   (Node's own guarantee is also best-effort in the presence of GC-copied
   buffers, just less commonly triggered).

## Other controls in place

- **RBAC**: whale-wallet mutations require `ADMIN`/`SUPERADMIN` role, checked
  both in the bot middleware and again inside `WhaleWalletAdminService`
  (defense in depth).
- **Audit log**: every whale-wallet add/approve/reject/remove/score-update
  writes an `AuditLog` row with actor, action, target, and metadata.
- **Rate limiting**: Redis-backed sliding window per Telegram user on all bot
  commands.
- **Secret redaction**: the logger (`structlog`) redacts known secret-shaped
  fields; never add a new field containing key material without adding it to
  `_REDACT_KEYS` in `utils/logger/__init__.py`.
- **Server-side risk enforcement**: `risk.evaluate_auto_trade` re-checks
  every limit (exposure, slippage, blacklist, cooldown, position count) at
  execution time, not just in the UI, so a compromised or modified client
  can't bypass them.
- **NEW — restart-safe trade reconciliation**: `engines/reconciliation.py`
  resolves any trade left `PENDING`/`SUBMITTED` by a crashed or redeployed
  process before the bot resumes normal operation, so a mid-flight swap can't
  silently double-submit or get lost. See `PORTING_NOTES.md` requirement #3.

## Reporting a vulnerability

Do not open a public GitHub issue for security reports. Use GitHub's private
security advisory feature on the repository, or a maintainer contact channel
you set up alongside this project.
