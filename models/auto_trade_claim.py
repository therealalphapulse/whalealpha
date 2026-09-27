"""
models/auto_trade_claim.py

Atomic per-(user, signal) idempotency claim for the standalone
Auto-Trade Engine (§9 of the spec: "Idempotency").

Structurally similar to models/auto_buy_claim.py (which belongs to the
existing, untouched domain/trading/real automation) but keyed on
signal_id rather than contract, per the spec's explicit "idempotency
key such as user_id + signal_id" guidance. Kept separate so the two
engines can never contend on each other's claims.

Status lifecycle: pending -> committed (buy confirmed on-chain and an
AutoTradePosition exists; permanent, never retried) or pending ->
failed -> {pending again | skipped}:

  "failed" means this attempt did not result in a buy. What happens
  next depends on whether the failure was retryable (see
  domain/trading/auto_trade/constants.py's RETRYABLE_REJECTION_REASONS
  and orchestrator.py's classification of execution-failure reasons):
    - Retryable (e.g. a transient RPC/on-chain hiccup): retry_count is
      incremented and the claim is left "failed", eligible to be
      reopened back to "pending" by claims.try_claim() once
      CLAIM_RETRY_BACKOFF_SECONDS has passed -- bounded by
      MAX_CLAIM_RETRY_ATTEMPTS, after which it is marked "skipped".
    - Not retryable (e.g. insufficient balance, daily limit reached --
      a deterministic outcome that won't change a few seconds later):
      marked "skipped" immediately, once.

  "skipped" is terminal, exactly like "committed": never reopened for
  this exact signal_id. This is what stops an engine-level condition
  (e.g. an underfunded wallet) from producing a flood of repeat
  auto-buy attempts/notifications for the same signal every scan tick
  -- a later signal for the same token gets its own, fresh claim.
"""

from sqlalchemy import Column, BigInteger, String, Integer, DateTime, func, UniqueConstraint
from infra.db.session import Base


class AutoTradeClaim(Base):
    __tablename__ = "auto_trade_claims"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    signal_id = Column(BigInteger, nullable=False, index=True)
    contract = Column(String, nullable=False, index=True)

    # "pending" | "committed" | "failed" | "skipped"
    status = Column(String, nullable=False, default="pending")

    # How many retryable attempts have already failed for this claim.
    # Only advances "failed" claims classified as retryable; a
    # non-retryable failure goes straight to "skipped" without
    # consuming a retry.
    retry_count = Column(Integer, nullable=False, default=0)
    # Last rejection/failure reason recorded against this claim, for
    # diagnostics (e.g. shown in /autotrade status, logs).
    last_reason = Column(String, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "signal_id", name="uq_auto_trade_claims_user_signal"),
    )

    def __repr__(self):
        return f"<AutoTradeClaim user={self.user_id} signal={self.signal_id} status={self.status}>"
