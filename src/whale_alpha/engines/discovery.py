"""Whale Wallet Discovery & Intelligence Engine — Hybrid Discovery Engine
(Phase 1 refactor). Originally new feature, no TS equivalent existed; this
closes the "users must never manually add whale wallets, so *something* has
to" gap — before this, `whale_wallets` only grew via a human admin's
/addwhale.

Pipeline (see docs/ARCHITECTURE.md for the full data-flow diagram):

    Candidate sourcing — MULTIPLE independent, priority-ordered streams, see
    discover_candidates below:
      1. Real-time on-chain launch discovery (pump.fun, LaunchLab, Raydium,
         Meteora — integrations/free_market_sources.py). Needs no tracked
         wallets, Signals, or API key; this is what actually eliminates the
         cold-start loop from zero.
      2. Trending-token provider fallback chain (Jupiter -> Birdeye ->
         DexScreener), also independent of anything already tracked.
      3. The legacy signaled-token-holders stream
         (integrations.wallet_discovery_source.find_candidates_from_token_holders)
         — co-buyers of tokens our own tracked whales' Signals already
         picked. High precision, but alone can never bootstrap from zero.
      4. Wallet Graph Expansion (engines/wallet_graph.py, its own phase in
         run_discovery_cycle) — every promoted wallet becomes a discovery
         node; repeated co-trading relationships raise confidence.
    One failing/disabled source never stops the others — see
    discover_candidates and run_discovery_cycle's per-phase try/except.
              |
              v
    WalletCandidate (staging table — engines/discovery.discover_candidates)
              |
              v  (fetch on-chain history, engines/discovery_metrics.compute_wallet_metrics)
              |  (score, engines/scoring.score_wallet — UNCHANGED algorithm)
              |  (behaviour, engines/behavior_scoring.py — Priority 6, enrichment only)
              |  (labels, engines/wallet_labels.py — Priority 7)
              v
    evaluate_promotion() -- pure decision --> promote via WhaleWalletAdminService
              |
              v
    WhaleWallet (APPROVED, auto_discovered=True, tags=smart-money labels) -->
    already wired into the signal engine (engines/monitor.py only ingests
    events from APPROVED wallets; engines/signal.py already weights
    confidence by wallet.score — no changes needed there, see
    docs/ARCHITECTURE.md).

A second, independent pass (`rescore_tracked_wallets`) periodically
re-evaluates already-APPROVED wallets and retires ones that go dormant or
stay unprofitable, freeing capacity for new discoveries — see
evaluate_retention().

Every promotion/retirement decision is a pure function
(evaluate_promotion / evaluate_retention / select_wallets_to_retire_for_ceiling)
so the actual admission-control logic is unit-testable without a database or
network, same testability shape as engines/scoring.py and engines/signal.py.
The impure orchestration around them (fetching candidates, running the DB
transaction, calling the admin service) is intentionally thin.
"""


from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from solana.rpc.async_api import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from whale_alpha.config import Env
from whale_alpha.db.models import (
    CandidateStatus,
    Role,
    Signal,
    User,
    WalletCandidate,
    WalletStatus,
    WhaleWallet,
)
from whale_alpha.engines.behavior_scoring import (
    behavior_confidence_bonus,
    behavior_scores_to_json,
    compute_behavior_scores,
)
from whale_alpha.engines.discovery_metrics import ComputedMetrics, compute_wallet_metrics
from whale_alpha.engines.scoring import MIN_APPROVED_SCORE, WalletMetrics, score_wallet
from whale_alpha.engines.wallet_graph import expand_wallet_graph
from whale_alpha.engines.wallet_labels import assign_labels
from whale_alpha.integrations import free_market_sources, price_feed
from whale_alpha.integrations.solana_connection import is_valid_solana_address
from whale_alpha.integrations.wallet_discovery_source import (
    DiscoveredCandidate,
    estimate_wallet_age_days,
    fetch_wallet_swap_history,
    find_candidates_from_token_holders,
    find_candidates_from_trending_tokens,
)
from whale_alpha.services.admin.whale_wallet_admin_service import (
    DISCOVERY_ENGINE_TELEGRAM_ID,
    Actor,
    WhaleWalletAdminService,
)
from whale_alpha.utils.logger import child_logger

log = child_logger("discoveryEngine")


# --------------------------------------------------------------------------
# Pure decision logic — no I/O, fully unit-testable.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryConfig:
    """Subset of Env consumed by the pure decision functions below, mirroring
    the SignalEngineConfig / AutoTradingRules pattern elsewhere in engines/
    so tests can construct one directly without a full pydantic Env."""

    min_tracked_wallets: int
    max_tracked_wallets: int
    min_score_to_approve: float
    min_roi_30d: float
    min_win_rate: float
    min_trade_count_30d: int
    min_wallet_age_days: int
    inactivity_timeout_days: int
    low_score_cycles_before_retire: int

    @classmethod
    def from_env(cls, env: Env) -> DiscoveryConfig:
        return cls(
            min_tracked_wallets=env.DISCOVERY_MIN_TRACKED_WALLETS,
            max_tracked_wallets=env.DISCOVERY_MAX_TRACKED_WALLETS,
            min_score_to_approve=env.DISCOVERY_MIN_SCORE_TO_APPROVE,
            min_roi_30d=env.DISCOVERY_MIN_ROI_30D,
            min_win_rate=env.DISCOVERY_MIN_WIN_RATE,
            min_trade_count_30d=env.DISCOVERY_MIN_TRADE_COUNT_30D,
            min_wallet_age_days=env.DISCOVERY_MIN_WALLET_AGE_DAYS,
            inactivity_timeout_days=env.DISCOVERY_INACTIVITY_TIMEOUT_DAYS,
            low_score_cycles_before_retire=env.DISCOVERY_LOW_SCORE_CYCLES_BEFORE_RETIRE,
        )


@dataclass(frozen=True)
class PromotionDecision:
    approved: bool
    reason: str | None  # populated when NOT approved; explains the disqualifying gate


def evaluate_promotion(
    *,
    score: float,
    trade_count_30d: int,
    metrics: WalletMetrics,
    flags: list[str],
    config: DiscoveryConfig,
) -> PromotionDecision:
    """A brand-new candidate must clear every gate below to be auto-promoted.
    Deliberately stricter (and more explicit) than the bar for an existing
    tracked wallet to stay APPROVED (scoring.MIN_APPROVED_SCORE alone) — a
    wallet with no track record in our own system yet has to prove more.
    """
    if "SUSPECTED_WASH_TRADING_FREQUENCY" in flags:
        return PromotionDecision(False, "SUSPECTED_WASH_TRADING")
    if metrics.wallet_age_days < config.min_wallet_age_days:
        return PromotionDecision(False, "WALLET_TOO_NEW")
    if trade_count_30d < config.min_trade_count_30d:
        return PromotionDecision(False, "INSUFFICIENT_TRADE_HISTORY")
    if metrics.win_rate < config.min_win_rate:
        return PromotionDecision(False, "WIN_RATE_BELOW_MINIMUM")
    if metrics.roi_30d < config.min_roi_30d:
        return PromotionDecision(False, "ROI_BELOW_MINIMUM")
    if score < config.min_score_to_approve:
        return PromotionDecision(False, "SCORE_BELOW_MINIMUM")
    return PromotionDecision(True, None)


@dataclass(frozen=True)
class RetentionDecision:
    retire: bool
    reason: str | None
    new_consecutive_low_score_cycles: int


def evaluate_retention(
    *,
    score: float | None,
    consecutive_low_score_cycles: int,
    last_active_at: datetime | None,
    now: datetime,
    config: DiscoveryConfig,
    allow_low_score_retirement: bool = True,
) -> RetentionDecision:
    """Decides whether an already-tracked wallet should be retired.

    Two independent triggers:
      * Inactivity: no observed on-chain buy (via the webhook -> monitor.py
        path, which already stamps `last_active_at`) for
        `inactivity_timeout_days`. Checked regardless of score, and
        regardless of `allow_low_score_retirement` — a wallet that's gone
        silent isn't contributing signal quality either way, so keeping it
        never helps the population-floor problem.
      * Sustained low score: `score` below scoring.MIN_APPROVED_SCORE for
        `low_score_cycles_before_retire` *consecutive* cycles (hysteresis —
        one noisy cycle shouldn't flip a good wallet out). Skipped entirely
        when `allow_low_score_retirement` is False, which the orchestration
        layer sets when the tracked population is already at/below the
        configured floor — removing a mediocre wallet when we're short on
        wallets would make the shortage worse, not better.
    """
    if last_active_at is not None and now - last_active_at > timedelta(days=config.inactivity_timeout_days):
        return RetentionDecision(True, "INACTIVITY", consecutive_low_score_cycles)

    if score is None:
        # No fresh score this cycle (e.g. no history provider configured) —
        # leave the streak counter untouched rather than guessing.
        return RetentionDecision(False, None, consecutive_low_score_cycles)

    if score < MIN_APPROVED_SCORE:
        streak = consecutive_low_score_cycles + 1
        if allow_low_score_retirement and streak >= config.low_score_cycles_before_retire:
            return RetentionDecision(True, "SUSTAINED_LOW_SCORE", streak)
        return RetentionDecision(False, None, streak)

    return RetentionDecision(False, None, 0)


def select_wallets_to_retire_for_ceiling(
    approved: list[tuple[str, float]],  # (wallet_id, score)
    max_tracked: int,
) -> list[str]:
    """If the tracked population exceeds max_tracked, returns the ids of the
    lowest-scoring wallets to retire to bring it back within bounds. Ties
    broken by list order (callers should pass a stable order, e.g. by id).
    """
    surplus = len(approved) - max_tracked
    if surplus <= 0:
        return []
    ranked = sorted(approved, key=lambda w: w[1])  # ascending score, worst first
    return [wallet_id for wallet_id, _score in ranked[:surplus]]


# --------------------------------------------------------------------------
# Orchestration — I/O around the pure functions above.
# --------------------------------------------------------------------------


async def _system_actor(session: AsyncSession) -> Actor:
    """Fetches or creates the system user the discovery engine acts as (see
    whale_wallet_admin_service.DISCOVERY_ENGINE_TELEGRAM_ID). SUPERADMIN role
    so it clears WhaleWalletAdminService's _assert_admin exactly like a human
    admin would — the engine IS the automated administrator for this table,
    per the "only administrators may add/override wallets" requirement.
    """
    result = await session.execute(select(User).where(User.telegram_id == DISCOVERY_ENGINE_TELEGRAM_ID))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(telegram_id=DISCOVERY_ENGINE_TELEGRAM_ID, role=Role.SUPERADMIN)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return Actor(id=user.id, role=user.role)


# Priority-ordered on-chain launch sources (Priority 1 — see
# integrations/free_market_sources.py). Each entry is (source_key,
# async_fn) where async_fn(http_client, connection, env, *,
# max_launches, max_holders_per_token) -> list[DiscoveredCandidate]. Tried
# in this order every cycle; one failing/disabled source never blocks the
# next — see discover_candidates.
_ON_CHAIN_LAUNCH_SOURCES = (
    ("pumpfun_launch", free_market_sources.find_candidates_from_pumpfun_launches),
    ("launchlab_launch", free_market_sources.find_candidates_from_launchlab_launches),
    ("raydium_new_pool", free_market_sources.find_candidates_from_raydium_new_pools),
    ("meteora_new_pool", free_market_sources.find_candidates_from_meteora_pools),
)


async def discover_candidates(
    session: AsyncSession, http_client: httpx.AsyncClient, connection: AsyncClient, env: Env
) -> dict[str, int]:
    """Sources new candidate addresses from every enabled discovery stream,
    in priority order, and queues them in WalletCandidate — skipping
    anything already tracked or already queued. Returns a per-source count
    of genuinely new candidates queued (see run_discovery_cycle for the
    structured log this feeds).

    Priority order (see docs/ARCHITECTURE.md and the Phase 1 refactor
    prompt for the full rationale):

      1. Real-time on-chain launch discovery (_ON_CHAIN_LAUNCH_SOURCES:
         pump.fun, LaunchLab, Raydium, Meteora) — fresh liquidity events and
         their early accumulators. Needs no tracked wallets, no Signals, no
         API key. This is what actually closes the cold-start loop from
         absolute zero, not just the trending-token bootstrap alone.

      2. Trending-token provider fallback chain (Jupiter -> Birdeye ->
         DexScreener, see integrations/free_market_sources.py). Also
         independent of anything already tracked.

      3. The legacy signaled-token-holders stream
         (`_queue_candidates_from_signaled_tokens`) — co-buyers of tokens
         our own tracked whales already picked. High-precision, but by
         itself can never produce a first candidate from zero tracked
         wallets: zero wallets -> zero Signals -> zero token mints to
         search. Kept running as ONE source among many, not the primary
         one, per the hybrid architecture.

      4. Wallet Graph Expansion (engines/wallet_graph.py) runs as its own
         phase in run_discovery_cycle, not here — it needs already-APPROVED
         wallets to expand from, so it naturally only contributes once the
         population above zero exists.

    Each source draws from one shared, priority-ordered budget
    (`DISCOVERY_CANDIDATE_BATCH_SIZE`): higher-priority sources get first
    claim on it, and whatever they don't use rolls over to the next source,
    rather than a fixed even split — so on a quiet on-chain-launch cycle the
    trending/legacy streams still get to use the leftover budget, and on a
    busy one the on-chain streams aren't artificially capped.
    """
    existing_wallets = await session.execute(select(WhaleWallet.address))
    existing_candidates = await session.execute(select(WalletCandidate.address))
    known_addresses = {row[0] for row in existing_wallets.all()} | {row[0] for row in existing_candidates.all()}

    remaining = env.DISCOVERY_CANDIDATE_BATCH_SIZE
    per_source_queued: dict[str, int] = {}

    for source_key, source_fn in _ON_CHAIN_LAUNCH_SOURCES:
        if remaining <= 0:
            break
        try:
            candidates = await source_fn(
                http_client,
                connection,
                env,
                max_launches=env.DISCOVERY_MAX_LAUNCHES_PER_SOURCE,
                max_holders_per_token=env.DISCOVERY_MAX_HOLDERS_PER_TOKEN,
            )
        except Exception as err:  # noqa: BLE001 — one provider failing must never stop the others
            log.warning("Discovery source failed, continuing with the rest", source=source_key, err=str(err))
            candidates = []
        found, queued = _queue_new_candidates(session, candidates, known_addresses, remaining)
        per_source_queued[source_key] = queued
        remaining -= queued
        log.info("Discovery source cycle", source=source_key, found=found, queued=queued)

    if remaining > 0 and env.DISCOVERY_TRENDING_ENABLED:
        found, queued = await _queue_trending_bootstrap_candidates(
            session, http_client, connection, env, known_addresses, budget=remaining
        )
        remaining -= queued
        # find_candidates_from_trending_tokens / the fallback chain both tag
        # each DiscoveredCandidate with the provider that actually served
        # it (trending_token_holder / birdeye_trending / dexscreener_trending)
        # rather than a generic "trending" bucket — see
        # _queue_trending_bootstrap_candidates.
        per_source_queued["trending_fallback_chain"] = queued
        log.info("Discovery source cycle", source="trending_fallback_chain", found=found, queued=queued)

    if remaining > 0:
        queued = await _queue_candidates_from_signaled_tokens(
            session, connection, env, known_addresses, budget=remaining
        )
        per_source_queued["token_holder_of_signaled_token"] = queued
        remaining -= queued
        log.info("Discovery source cycle", source="token_holder_of_signaled_token", queued=queued)

    if sum(per_source_queued.values()):
        await session.commit()
    return per_source_queued


async def _queue_candidates_from_signaled_tokens(
    session: AsyncSession,
    connection: AsyncClient,
    env: Env,
    known_addresses: set[str],
    *,
    budget: int,
) -> int:
    """Priority 3 — the legacy stream: other large holders of tokens our
    own tracked whales' Signals already picked. See discover_candidates'
    docstring for why this is now one source among several rather than the
    primary (or only) one.
    """
    recent_signals = await session.execute(
        select(Signal.token_mint).order_by(Signal.created_at.desc()).limit(env.DISCOVERY_SOURCE_TOKEN_LOOKBACK)
    )
    token_mints = list({row[0] for row in recent_signals.all()})
    if not token_mints:
        log.debug("No recent signals to source discovery candidates from")
        return 0

    queued = 0
    for token_mint in token_mints:
        if queued >= budget:
            break
        candidates = await find_candidates_from_token_holders(
            connection,
            token_mint,
            env.DISCOVERY_MAX_HOLDERS_PER_TOKEN,
            min_interval_seconds=env.DISCOVERY_RPC_MIN_INTERVAL_SECONDS,
            max_retries=env.DISCOVERY_RPC_MAX_RETRIES,
        )
        _, newly_queued = _queue_new_candidates(session, candidates, known_addresses, budget - queued)
        queued += newly_queued
    return queued


async def _queue_trending_bootstrap_candidates(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    connection: AsyncClient,
    env: Env,
    known_addresses: set[str],
    *,
    budget: int,
) -> tuple[int, int]:
    """Priority 2 — Jupiter Tokens API V2 first (unchanged, existing
    behavior); if that yields nothing (no key configured, or the request
    failed), falls through to the Birdeye -> DexScreener chain in
    integrations/free_market_sources.py. Returns (found, queued).
    """
    if budget <= 0:
        return 0, 0

    jupiter_candidates = await find_candidates_from_trending_tokens(
        http_client,
        connection,
        env,
        max_tokens=env.DISCOVERY_TRENDING_TOKEN_LIMIT,
        max_holders_per_token=env.DISCOVERY_MAX_HOLDERS_PER_TOKEN,
    )
    if jupiter_candidates:
        found, queued = _queue_new_candidates(session, jupiter_candidates, known_addresses, budget)
        return found, queued

    fallback_candidates = await free_market_sources.find_trending_tokens_multi_provider(
        http_client,
        connection,
        env,
        max_tokens=env.DISCOVERY_TRENDING_TOKEN_LIMIT,
        max_holders_per_token=env.DISCOVERY_MAX_HOLDERS_PER_TOKEN,
        jupiter_mints=None,
    )
    return _queue_new_candidates(session, fallback_candidates, known_addresses, budget)


def _queue_new_candidates(
    session: AsyncSession,
    candidates: list[DiscoveredCandidate],
    known_addresses: set[str],
    budget: int,
) -> tuple[int, int]:
    """Shared dedup/validation/insert logic for every sourcing stream — adds
    a WalletCandidate row (in-session, not yet committed) for each address
    not already tracked or queued, up to `budget`. Mutates `known_addresses`
    in place so sources called sequentially within one cycle never
    double-queue the same address. Returns (found, queued) — `found` is the
    number of raw candidates the source returned (before dedup/budget/
    validation), `queued` is how many actually became new rows; the gap
    between them is what the "Rejected: Duplicate" bucket in the discovery
    log reflects.
    """
    queued = 0
    for candidate in candidates:
        if queued >= budget:
            break
        if candidate.address in known_addresses:
            continue
        if not is_valid_solana_address(candidate.address):
            continue
        known_addresses.add(candidate.address)
        session.add(
            WalletCandidate(
                address=candidate.address,
                source=candidate.source,
                discovered_from_token_mint=candidate.discovered_from_token_mint,
            )
        )
        queued += 1
        log.info(
            "New wallet discovered",
            address=candidate.address,
            source=candidate.source,
            token_mint=candidate.discovered_from_token_mint,
        )
    return len(candidates), queued


async def evaluate_candidates(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    connection: AsyncClient,
    env: Env,
    actor: Actor,
) -> dict[str, int]:
    """Fetches history + scores a batch of un-evaluated/stale candidates,
    promoting the ones that clear evaluate_promotion() into whale_wallets
    (capacity permitting) and marking the rest EVALUATED/REJECTED so they
    aren't immediately re-fetched next cycle.

    For every candidate that produces enough history to score (Priority 5),
    also computes on-chain behaviour scores (Priority 6,
    engines/behavior_scoring.py) and derives smart-money labels from them
    (Priority 7, engines/wallet_labels.py) — stored on the candidate
    regardless of outcome, and copied onto WhaleWallet.tags if promoted.
    Behaviour only ever *enriches* confidence by a small, bounded amount
    (see behavior_confidence_bonus); it never overrides the core
    score_wallet output or any of evaluate_promotion's hard gates.

    Tracks a per-source pass/reject breakdown and a rejection-reason tally
    for the structured discovery log (see run_discovery_cycle).
    """
    config = DiscoveryConfig.from_env(env)
    summary = {"evaluated": 0, "promoted": 0, "rejected": 0, "insufficient_data": 0}
    by_source: dict[str, Counter] = {}
    reason_summary: Counter = Counter()

    def _source_counter(source: str) -> Counter:
        return by_source.setdefault(source, Counter())

    reeval_cutoff = datetime.now(UTC) - timedelta(hours=env.DISCOVERY_CANDIDATE_MIN_REEVAL_HOURS)
    result = await session.execute(
        select(WalletCandidate)
        .where(
            WalletCandidate.status.in_((CandidateStatus.NEW, CandidateStatus.EVALUATED)),
            (WalletCandidate.last_evaluated_at.is_(None)) | (WalletCandidate.last_evaluated_at < reeval_cutoff),
        )
        .order_by(WalletCandidate.first_seen_at.asc())
        .limit(env.DISCOVERY_CANDIDATE_BATCH_SIZE)
    )
    batch = list(result.scalars())
    if not batch:
        return summary

    current_approved_count = await _count_approved(session)

    sol_price_usd = await price_feed.get_sol_price_usd(http_client, env)
    if sol_price_usd is None:
        log.warning("SOL/USD price unavailable — skipping candidate evaluation this cycle")
        return summary

    for candidate in batch:
        summary["evaluated"] += 1
        _source_counter(candidate.source)["evaluated"] += 1
        candidate.evaluation_count += 1
        candidate.last_evaluated_at = datetime.now(UTC)

        swaps = await fetch_wallet_swap_history(
            http_client, env, candidate.address, sol_price_usd=sol_price_usd
        )
        if swaps is None:
            summary["insufficient_data"] += 1
            reason_summary["NO_HISTORY_PROVIDER_OR_FETCH_FAILED"] += 1
            candidate.status = CandidateStatus.EVALUATED
            candidate.rejection_reason = "NO_HISTORY_PROVIDER_OR_FETCH_FAILED"
            continue

        wallet_age_days = await estimate_wallet_age_days(connection, candidate.address)
        computed = compute_wallet_metrics(swaps, wallet_age_days=wallet_age_days)
        if computed is None:
            summary["insufficient_data"] += 1
            reason_summary["INSUFFICIENT_TRADE_HISTORY"] += 1
            candidate.status = CandidateStatus.EVALUATED
            candidate.rejection_reason = "INSUFFICIENT_TRADE_HISTORY"
            continue

        result_ = score_wallet(computed.metrics)
        behavior = compute_behavior_scores(computed.metrics, swaps)
        labels = assign_labels(
            metrics=computed.metrics, behavior=behavior, trade_count_30d=computed.trade_count_30d
        )
        enriched_confidence = max(0.0, min(100.0, result_.confidence + behavior_confidence_bonus(behavior)))

        candidate.last_score = result_.score
        candidate.last_confidence = enriched_confidence
        candidate.last_metrics = _metrics_to_json(computed)
        candidate.behavior_scores = behavior_scores_to_json(behavior)
        candidate.labels = labels

        decision = evaluate_promotion(
            score=result_.score,
            trade_count_30d=computed.trade_count_30d,
            metrics=computed.metrics,
            flags=result_.flags,
            config=config,
        )

        if not decision.approved:
            summary["rejected"] += 1
            _source_counter(candidate.source)["rejected"] += 1
            reason_summary[decision.reason or "UNKNOWN"] += 1
            candidate.status = CandidateStatus.EVALUATED
            candidate.rejection_reason = decision.reason
            continue

        if current_approved_count >= config.max_tracked_wallets:
            # Clears every quality bar but there's no room. Leave it
            # EVALUATED (not REJECTED — it's good, just unlucky on timing) so
            # it's picked up again once a slot opens from retirement/ceiling
            # enforcement, without needing to re-fetch its history immediately.
            summary["rejected"] += 1
            _source_counter(candidate.source)["rejected"] += 1
            reason_summary["AT_MAX_TRACKED_WALLETS"] += 1
            candidate.status = CandidateStatus.EVALUATED
            candidate.rejection_reason = "AT_MAX_TRACKED_WALLETS"
            continue

        admin_service = WhaleWalletAdminService(session)
        wallet = await admin_service.promote_candidate(
            actor,
            address=candidate.address,
            label=None,
            score=result_.score,
            confidence=enriched_confidence,
            metrics=computed.metrics,
            source=candidate.source,
            tags=labels,
        )
        candidate.status = CandidateStatus.PROMOTED
        candidate.promoted_wallet_id = wallet.id
        current_approved_count += 1
        summary["promoted"] += 1
        _source_counter(candidate.source)["promoted"] += 1
        log.info(
            "Wallet promoted",
            address=candidate.address,
            score=result_.score,
            wallet_id=wallet.id,
            labels=labels,
        )

    await session.commit()

    if by_source:
        log.info(
            "Discovery evaluation breakdown",
            by_source={source: dict(counts) for source, counts in by_source.items()},
            reason_summary=dict(reason_summary),
        )

    return summary


async def rescore_tracked_wallets(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    env: Env,
    actor: Actor,
) -> dict[str, int]:
    """Periodically re-fetches history and re-scores already-APPROVED
    wallets (oldest-scored first), retiring ones that go dormant or stay
    unprofitable. See evaluate_retention() for the retirement rules.
    """
    config = DiscoveryConfig.from_env(env)
    summary = {"rescored": 0, "retired_inactive": 0, "retired_low_score": 0}

    result = await session.execute(
        select(WhaleWallet)
        .where(WhaleWallet.status == WalletStatus.APPROVED)
        .order_by(WhaleWallet.last_scored_at.asc().nulls_first())
        .limit(env.DISCOVERY_RESCORE_BATCH_SIZE)
    )
    batch = list(result.scalars())
    if not batch:
        return summary

    current_approved_count = await _count_approved(session)
    allow_low_score_retirement = current_approved_count > config.min_tracked_wallets

    sol_price_usd = await price_feed.get_sol_price_usd(http_client, env)

    admin_service = WhaleWalletAdminService(session)
    now = datetime.now(UTC)

    for wallet in batch:
        fresh_score: float | None = None
        fresh_confidence = wallet.confidence
        fresh_metrics: WalletMetrics | None = None

        if sol_price_usd is not None:
            swaps = await fetch_wallet_swap_history(
                http_client, env, wallet.address, sol_price_usd=sol_price_usd
            )
            if swaps is not None:
                # wallet_age_days on the row is a point-in-time snapshot from
                # the last time we actually measured it (promotion or a prior
                # rescore) — advance it by elapsed time rather than reusing a
                # stale number, without spending an RPC call to re-derive it
                # from scratch every cycle.
                age_estimate = wallet.wallet_age_days
                if age_estimate is not None:
                    anchor = wallet.last_scored_at or wallet.added_at
                    if anchor is not None:
                        age_estimate += max(0, (now - anchor).days)

                computed: ComputedMetrics | None = compute_wallet_metrics(
                    swaps, wallet_age_days=age_estimate
                )
                if computed is not None:
                    result_ = score_wallet(computed.metrics)
                    fresh_score = result_.score
                    fresh_confidence = result_.confidence
                    fresh_metrics = computed.metrics

        decision = evaluate_retention(
            score=fresh_score,
            consecutive_low_score_cycles=wallet.consecutive_low_score_cycles,
            last_active_at=wallet.last_active_at,
            now=now,
            config=config,
            allow_low_score_retirement=allow_low_score_retirement,
        )

        if fresh_metrics is not None and fresh_score is not None:
            await admin_service.update_metrics(
                actor,
                wallet.id,
                score=fresh_score,
                confidence=fresh_confidence,
                metrics=fresh_metrics,
                consecutive_low_score_cycles=decision.new_consecutive_low_score_cycles,
            )
            summary["rescored"] += 1
            log.info("Wallet score updated", wallet_id=wallet.id, score=fresh_score)

        if decision.retire:
            await admin_service.set_status(actor, wallet.id, WalletStatus.RETIRED)
            current_approved_count -= 1
            allow_low_score_retirement = current_approved_count > config.min_tracked_wallets
            key = "retired_inactive" if decision.reason == "INACTIVITY" else "retired_low_score"
            summary[key] += 1
            log.info("Wallet removed", wallet_id=wallet.id, reason=decision.reason)

    return summary


async def enforce_population_ceiling(session: AsyncSession, env: Env, actor: Actor) -> int:
    """If APPROVED count exceeds DISCOVERY_MAX_TRACKED_WALLETS (e.g. a burst
    of promotions in one cycle), retires the lowest-scoring wallets down to
    the ceiling. Does nothing when at/under the ceiling.
    """
    result = await session.execute(
        select(WhaleWallet.id, WhaleWallet.score)
        .where(WhaleWallet.status == WalletStatus.APPROVED)
        .order_by(WhaleWallet.id)
    )
    approved = [(row[0], row[1]) for row in result.all()]
    to_retire = select_wallets_to_retire_for_ceiling(approved, env.DISCOVERY_MAX_TRACKED_WALLETS)

    admin_service = WhaleWalletAdminService(session)
    for wallet_id in to_retire:
        await admin_service.set_status(actor, wallet_id, WalletStatus.RETIRED)
        log.info("Wallet removed", wallet_id=wallet_id, reason="POPULATION_CEILING")

    return len(to_retire)


async def _count_approved(session: AsyncSession) -> int:
    result = await session.execute(
        select(WhaleWallet.id).where(WhaleWallet.status == WalletStatus.APPROVED)
    )
    return len(result.all())


def _metrics_to_json(computed: ComputedMetrics) -> dict[str, float | int]:
    m = computed.metrics
    return {
        "roi_30d": m.roi_30d,
        "win_rate": m.win_rate,
        "pnl_usd_30d": m.pnl_usd_30d,
        "avg_hold_minutes": m.avg_hold_minutes,
        "avg_position_usd": m.avg_position_usd,
        "trade_frequency_7d": m.trade_frequency_7d,
        "wallet_age_days": m.wallet_age_days,
        "max_drawdown": m.max_drawdown,
        "trade_success_rate": m.trade_success_rate,
        "trade_count_30d": computed.trade_count_30d,
    }


async def run_discovery_cycle(
    env: Env,
    session_factory: async_sessionmaker,
    http_client: httpx.AsyncClient,
    solana_connection: AsyncClient,
) -> None:
    log.info("Discovery cycle started")

    async with session_factory() as session:
        actor = await _system_actor(session)

    summary: dict[str, object] = {}

    try:
        async with session_factory() as session:
            by_source_queued = await discover_candidates(session, http_client, solana_connection, env)
            summary["candidates_queued"] = sum(by_source_queued.values())
            summary["candidates_by_source"] = by_source_queued
    except Exception as err:  # noqa: BLE001 — one phase failing shouldn't block the others
        log.error("Discovery: candidate sourcing failed", err=str(err))

    try:
        async with session_factory() as session:
            eval_summary = await evaluate_candidates(session, http_client, solana_connection, env, actor)
            summary.update(eval_summary)
    except Exception as err:  # noqa: BLE001
        log.error("Discovery: candidate evaluation failed", err=str(err))

    try:
        async with session_factory() as session:
            rescore_summary = await rescore_tracked_wallets(session, http_client, env, actor)
            summary.update(rescore_summary)
    except Exception as err:  # noqa: BLE001
        log.error("Discovery: re-scoring pass failed", err=str(err))

    if env.DISCOVERY_GRAPH_EXPANSION_ENABLED:
        try:
            async with session_factory() as session:
                existing_wallets = await session.execute(select(WhaleWallet.address))
                existing_candidates = await session.execute(select(WalletCandidate.address))
                known_addresses = {row[0] for row in existing_wallets.all()} | {
                    row[0] for row in existing_candidates.all()
                }
                graph_candidates, wallets_processed = await expand_wallet_graph(
                    session, http_client, solana_connection, env, known_addresses
                )
                _, graph_queued = _queue_new_candidates(
                    session, graph_candidates, known_addresses, budget=len(graph_candidates)
                )
                if graph_queued:
                    await session.commit()
                summary["graph_expanded"] = wallets_processed
                summary["graph_candidates_queued"] = graph_queued
        except Exception as err:  # noqa: BLE001
            log.error("Discovery: wallet graph expansion failed", err=str(err))

    try:
        async with session_factory() as session:
            summary["retired_ceiling"] = await enforce_population_ceiling(session, env, actor)
    except Exception as err:  # noqa: BLE001
        log.error("Discovery: population ceiling enforcement failed", err=str(err))

    try:
        async with session_factory() as session:
            summary["tracked_wallets"] = await _count_approved(session)
            if summary["tracked_wallets"] < env.DISCOVERY_MIN_TRACKED_WALLETS:
                log.warning(
                    "Tracked wallet population below configured floor — "
                    "more profitable candidates are needed, cannot be manufactured",
                    tracked=summary["tracked_wallets"],
                    floor=env.DISCOVERY_MIN_TRACKED_WALLETS,
                )
    except Exception as err:  # noqa: BLE001
        log.error("Discovery: population count check failed", err=str(err))

    log.info("Discovery cycle completed", **summary)


def start_discovery_loop(
    env: Env,
    session_factory: async_sessionmaker,
    http_client: httpx.AsyncClient,
    solana_connection: AsyncClient,
):
    """Same asyncio-task-and-sleep-loop shape as engines/scheduler.py and
    engines/price_alerts.py — see those modules' docstrings for why (and
    for the arq/celery-beat upgrade path once running multiple workers)."""

    async def _loop() -> None:
        while True:
            await asyncio.sleep(env.DISCOVERY_INTERVAL_SECONDS)
            try:
                await run_discovery_cycle(env, session_factory, http_client, solana_connection)
            except Exception as err:  # noqa: BLE001 — mirrors the other loops' catch-all
                log.error("Discovery cycle crashed", err=str(err))

    task = asyncio.create_task(_loop())

    async def stop() -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return stop
