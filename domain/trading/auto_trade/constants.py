"""domain/trading/auto_trade/constants.py

Shared, dependency-free constants for the Auto-Trade Engine: rejection
reasons (§6) and default tuning values. Kept in its own module so
policy_service.py, risk_gate.py, and orchestrator.py can share it
without import cycles.
"""

DEFAULT_ETH_PER_TRADE = 0.01
DEFAULT_ETH_PER_TRADE = DEFAULT_ETH_PER_TRADE
DEFAULT_DAILY_TRADE_LIMIT = 5
DEFAULT_MAX_OPEN_POSITIONS = 5
DEFAULT_COOLDOWN_SECONDS = 120
BUY_NETWORK_RESERVE_WEI = 2_000_000_000_000_000  # ~0.002 ETH gas/fee reserve

SIGNAL_LOOKBACK_MINUTES = 20
CLAIM_RECONCILE_GRACE_SECONDS = 90

# Bounded retry policy for claim failures classified as *retryable*
# (transient/technical -- e.g. an RPC call failing), applied by
# claims.finalize_claim() / claims.try_claim(). A deterministic
# rejection (insufficient balance, daily limit, etc.) is never
# retryable and skips this entirely -- see claims.py's module
# docstring and orchestrator.py's classification of failure reasons.
MAX_CLAIM_RETRY_ATTEMPTS = 3
CLAIM_RETRY_BACKOFF_SECONDS = 60

# How long the Auto-Trade Engine stops evaluating new signals for a
# user after a deterministic, account-level block that a few seconds
# (or even a few minutes) of retrying cannot fix -- currently
# insufficient wallet balance / no wallet. This is a circuit breaker
# at the wallet level, separate from and in addition to per-signal
# claim retries: without it, a *different* qualifying signal arriving
# while the wallet is still underfunded would still trigger its own
# fresh rejection + notification. One pause covers every signal until
# it expires or the user tops up and a later cycle finds sufficient
# balance again.
INSUFFICIENT_BALANCE_PAUSE_MINUTES = 15


class RejectionReason:
    AUTO_TRADE_DISABLED = "AUTO_TRADE_DISABLED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    SIGNAL_TIER_NOT_ALLOWED = "SIGNAL_TIER_NOT_ALLOWED"
    SIGNAL_TOO_OLD = "SIGNAL_TOO_OLD"
    SIGNAL_BEFORE_ACTIVATION = "SIGNAL_BEFORE_ACTIVATION"
    DAILY_LIMIT_REACHED = "DAILY_LIMIT_REACHED"
    MAX_OPEN_POSITIONS_REACHED = "MAX_OPEN_POSITIONS_REACHED"
    MAX_EXPOSURE_REACHED = "MAX_EXPOSURE_REACHED"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    POSITION_ALREADY_OPEN = "POSITION_ALREADY_OPEN"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    INVALID_TRADE_AMOUNT = "INVALID_TRADE_AMOUNT"
    EXECUTION_UNAVAILABLE = "EXECUTION_UNAVAILABLE"
    RISK_CHECK_FAILED = "RISK_CHECK_FAILED"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    NO_WALLET = "NO_WALLET"


# §7 execution-risk-gate reasons that are transient/technical rather
# than a deterministic decision -- eligible for claims.py's bounded,
# backed-off retry instead of an immediate permanent skip. Everything
# else from evaluate_execution_risk (currently just INSUFFICIENT_BALANCE
# and NO_WALLET) is deterministic given the wallet's current state and
# goes through the wallet-level pause instead (see
# INSUFFICIENT_BALANCE_PAUSE_MINUTES) -- retrying seconds later can't
# change either answer, only time (funding the wallet) can.
RETRYABLE_REJECTION_REASONS = frozenset({RejectionReason.EXECUTION_UNAVAILABLE})


class ExecutionStatus:
    SUBMITTED = "submitted"
    CONFIRMED_SUCCESS = "confirmed_success"
    CONFIRMED_FAILURE = "confirmed_failure"
    UNKNOWN = "unknown"
