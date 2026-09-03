"""
Smart Wallet Discovery Engine.

Continuously discovers candidate Solana wallets for the Premium
Smart Money database, using every reliable signal already available
inside AlphaPulse (Blueprint requirement: "using all available project
integrations and reliable data sources") instead of any single feed:

  1. KOL provider wallets   — services/kol_tracker.py's synced KolWallet
                               table (already vetted, labeled traders).
  2. Independent Pump.fun   — pulls fresh Pump.fun mints directly from
     discovery + activity     the same public GeckoTerminal feed
                               services/pump_radar.py uses
                               (fetch_pump_fun_launches), checks each
                               one's own live liquidity/volume, and
                               surfaces top holders of the ones showing
                               real trading activity. THIS is the
                               engine's primary, always-on source — it
                               never reads models/signal_token.py and
                               has no dependency on the free Signal
                               Engine having alerted, or even seen,
                               anything. Discovery keeps running on a
                               day the public Signal Engine sends zero
                               alerts.
  3. Winning-signal holders — top holders of tokens that ALSO happened
                               to graduate to a big multiple in the free
                               Signal Engine (models/signal_token.py),
                               on the theory that large early holders of
                               repeated winners are disproportionately
                               likely to be skilled, not lucky. This is
                               a secondary, opportunistic bonus source
                               on top of #2 above, not a dependency —
                               discovery does not wait on it or require
                               it to produce anything.
  4. Popular tracked wallets — wallets multiple different AlphaPulse
                               users have chosen to track manually
                               (models/tracked_wallet.py); independent
                               user conviction is itself a weak signal.
  5. Peer discovery          — counter-parties that already-active elite
                               Premium wallets frequently transact
                               with/alongside (cheap graph expansion on
                               top of what the monitor loop already
                               fetches, no extra API calls).

Every candidate is de-duplicated against the existing premium_wallets
table, lightly validated (real wallet, minimum portfolio value, real
recent activity) before being inserted as status="candidate", and is
left for services/premium_wallet_scorer.py to actually score over time
(consistency, profitability, risk, etc.) and for
services/premium_wallet_maintenance.py to keep the database healthy
(probation, removal, automatic replacement). Premium Signal generation
(services/premium_signal_engine.py) only ever CONSUMES this database —
it does not feed it. No manual curation is required at any step.
"""

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select, func as sa_func, text

from infra.db.session import async_session, engine
from config.settings import (
    PREMIUM_DISCOVERY_BATCH_SIZE,
    PREMIUM_WALLET_HARD_CAP,
    PREMIUM_WALLET_INITIAL_TARGET,
    PREMIUM_BOOTSTRAP_DISCOVERY_INTERVAL_SECONDS,
    PREMIUM_WALLET_MIN_PORTFOLIO_USD,
    PREMIUM_DISCOVERY_MIN_WINNING_MULTIPLE,
    PREMIUM_DISCOVERY_MIN_TRACKED_WALLET_USERS,
    PREMIUM_WALLET_LOW_LIQUIDITY_USD,
)
from models.premium_wallet import PremiumWallet
from models.kol_wallet import KolWallet
from models.tracked_wallet import TrackedWallet
from models.signal_token import SignalToken
from providers.rpc.helius import get_recent_signatures
from domain.intelligence.wallet_intelligence import fetch_wallet_assets, get_sol_price_usd
from domain.intelligence.holders import get_holder_analysis
from providers.marketdata.dexscreener import get_token_card_info
# Reused, not rebuilt: the same public Pump.fun mint feed
# services/pump_radar.py already uses. Pulling from it directly here
# (rather than from models.signal_token.SignalToken) is exactly what
# makes this engine's primary discovery source independent of the free
# Signal Engine ever alerting on anything.
from domain.signals.pump_radar import fetch_pump_fun_launches

logger = logging.getLogger("AlphaPulse.PremiumDiscovery")


async def migrate_premium_schema() -> None:
    """
    Safe-to-run-repeatedly ALTER TABLE guard, same pattern used by every
    other module (see services/kol_tracker.migrate_kol_wallet_schema).
    create_all() only creates brand-new tables, so this exists purely to
    patch any older/partial deploy of the Premium tables — on a fresh
    database create_all() already produces the correct schema and every
    statement below is a harmless no-op.
    """
    statements = [
        "ALTER TABLE premium_memberships ADD COLUMN IF NOT EXISTS signal_alerts_enabled BOOLEAN DEFAULT TRUE",
        "ALTER TABLE premium_wallets ADD COLUMN IF NOT EXISTS notes TEXT",
        "ALTER TABLE premium_wallets ADD COLUMN IF NOT EXISTS classification VARCHAR",
        "ALTER TABLE premium_wallets ADD COLUMN IF NOT EXISTS position_size_score FLOAT",
        "ALTER TABLE premium_wallets ADD COLUMN IF NOT EXISTS liquidity_preference_score FLOAT",
        "ALTER TABLE premium_wallets ADD COLUMN IF NOT EXISTS scam_exposure_score FLOAT",
        "ALTER TABLE premium_wallets ADD COLUMN IF NOT EXISTS avg_position_usd FLOAT",
        "ALTER TABLE premium_wallets ADD COLUMN IF NOT EXISTS avg_entry_liquidity_usd FLOAT",
        "ALTER TABLE premium_wallets ADD COLUMN IF NOT EXISTS scam_exposure_pct FLOAT",
        "ALTER TABLE premium_wallet_trades ADD COLUMN IF NOT EXISTS entry_liquidity_usd FLOAT",
        "ALTER TABLE premium_wallet_trades ADD COLUMN IF NOT EXISTS is_flagged_risky VARCHAR",
        "ALTER TABLE premium_wallet_trades ADD COLUMN IF NOT EXISTS risk_flags VARCHAR",
    ]
    for stmt in statements:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
        except Exception as e:
            logger.warning(f"Premium schema migration statement skipped ({stmt}): {e}")

    logger.info("✅ Premium schema migration complete")


async def _existing_addresses(session) -> set[str]:
    res = await session.execute(select(PremiumWallet.wallet_address))
    return {row[0] for row in res.all()}


async def _current_wallet_count(session) -> int:
    res = await session.execute(
        select(sa_func.count()).select_from(PremiumWallet).where(PremiumWallet.status != "removed")
    )
    return res.scalar() or 0


async def _active_wallet_count(session) -> int:
    """Wallets actually qualified and contributing to Premium Intelligence
    today (status == "active") — this, not raw DB size, is what the
    Operational Threshold model (Bootstrap Discovery / Premium Integration)
    is measured against."""
    res = await session.execute(
        select(sa_func.count()).select_from(PremiumWallet).where(PremiumWallet.status == "active")
    )
    return res.scalar() or 0


# ---------------------------------------------------------------------
# Candidate source collectors — each returns {address: (source, detail)}
# ---------------------------------------------------------------------

async def _collect_from_kol_wallets(session, existing: set[str]) -> dict:
    candidates = {}
    res = await session.execute(select(KolWallet).where(KolWallet.active.is_(True)))
    for w in res.scalars().all():
        addr = (w.wallet_address or "").strip()
        if not addr or addr in existing:
            continue
        candidates[addr] = ("kol_provider", w.label or w.handle or "KOL")
    return candidates


async def _collect_from_independent_pump_discovery(
    session, existing: set[str], scan_limit: int = 20, top_holders_per_token: int = 8
) -> dict:
    """
    Fully independent Pump.fun discovery + activity-monitoring path
    (Blueprint Problem 3). Reuses services/pump_radar.fetch_pump_fun_
    launches() -- the same public GeckoTerminal feed the free Signal
    Engine scans -- to get fresh Pump.fun-origin mints directly, checks
    each one's own live liquidity/volume (services/dexscreener.
    get_token_card_info) to "monitor trading activity", and pulls
    holder analysis for the ones showing real activity to surface
    candidate wallets.

    Deliberately does NOT read models.signal_token.SignalToken or care
    whether the free Signal Engine has alerted, or even scored, any of
    these mints -- that's what makes this the engine's primary,
    always-on source. It keeps discovering wallets on a day the public
    Signal Engine sends zero alerts, same as any other day.

    Session param is accepted for signature symmetry with the other
    collectors but unused here (this source is pure external I/O, no
    DB reads of its own beyond the shared `existing` de-dupe set).
    """
    candidates = {}

    try:
        mints = await fetch_pump_fun_launches(limit=scan_limit)
    except Exception as e:
        logger.warning(f"Discovery source independent_pump_discovery fetch failed: {e}")
        return candidates

    def _num(value, default=0.0):
        try:
            if value is None:
                return default
            return float(str(value).replace(",", "").replace("$", ""))
        except (ValueError, TypeError):
            return default

    for mint in mints:
        if mint in existing:
            continue

        try:
            data = await asyncio.wait_for(get_token_card_info(mint), timeout=10)
        except Exception:
            data = None

        if not data:
            continue

        # "Monitor trading activity" — a light activity floor, not the
        # Signal Engine's own quality bar (pump_radar.py's
        # MIN_LIQUIDITY_USD/MIN_VOLUME_1H stay untouched and unrelated
        # to this). Reuses the existing low-liquidity threshold already
        # defined for Premium wallet scoring rather than inventing a
        # new constant.
        liq = _num(data.get("liquidity"))
        vol = _num(data.get("volume_1h"))
        if liq < PREMIUM_WALLET_LOW_LIQUIDITY_USD or vol <= 0:
            continue

        try:
            analysis = await asyncio.wait_for(get_holder_analysis(mint), timeout=12)
        except Exception as e:
            logger.debug(f"Independent pump discovery holder analysis failed for {mint}: {e}")
            continue

        if not analysis:
            continue

        top_addresses = analysis.get("top_holder_addresses") or []
        # Skip the very top holder — on Solana that's frequently the LP
        # pool / bonding curve / burn address, not a real trader (same
        # convention as _collect_from_winning_signals below).
        for addr in top_addresses[1 : 1 + top_holders_per_token]:
            addr = (addr or "").strip()
            if not addr or addr in existing or addr in candidates:
                continue
            candidates[addr] = (
                "independent_pump_discovery",
                f"early holder of Pump.fun token {data.get('symbol') or mint[:6]}",
            )

        await asyncio.sleep(0.4)  # gentle on the holder-analysis RPC

    return candidates


async def _collect_from_tracked_wallets(session, existing: set[str]) -> dict:
    """Wallets tracked by >= N distinct users — independent human conviction."""
    candidates = {}
    res = await session.execute(
        select(TrackedWallet.wallet_address, sa_func.count(sa_func.distinct(TrackedWallet.user_id)).label("n"))
        .group_by(TrackedWallet.wallet_address)
        .having(sa_func.count(sa_func.distinct(TrackedWallet.user_id)) >= PREMIUM_DISCOVERY_MIN_TRACKED_WALLET_USERS)
    )
    for addr, n in res.all():
        addr = (addr or "").strip()
        if not addr or addr in existing:
            continue
        candidates[addr] = ("tracked_wallet_popularity", f"tracked by {n} users")
    return candidates


async def _collect_from_winning_signals(session, existing: set[str], limit_tokens: int = 15) -> dict:
    """
    Top holders of the most recent tokens that hit a big multiple in the
    free Signal Engine. Cheap, reuses data AlphaPulse already tracks —
    no extra external accounts needed.
    """
    candidates = {}

    res = await session.execute(
        select(SignalToken)
        .where(SignalToken.ath_multiple >= PREMIUM_DISCOVERY_MIN_WINNING_MULTIPLE)
        .order_by(SignalToken.signaled_at.desc())
        .limit(limit_tokens)
    )
    winners = res.scalars().all()

    for token in winners:
        if not token.contract:
            continue
        try:
            analysis = await asyncio.wait_for(get_holder_analysis(token.contract), timeout=12)
        except Exception as e:
            logger.debug(f"Discovery holder analysis failed for {token.contract}: {e}")
            continue

        if not analysis:
            continue

        top_addresses = analysis.get("top_holder_addresses") or []
        # Skip the very top holder — on Solana that's frequently the LP
        # pool / bonding curve / burn address, not a real trader.
        for addr in top_addresses[1:8]:
            addr = (addr or "").strip()
            if not addr or addr in existing or addr in candidates:
                continue
            candidates[addr] = (
                "winning_signal_holder",
                f"top holder of {token.symbol or token.contract[:6]} ({(token.ath_multiple or 0):.1f}x)",
            )

        await asyncio.sleep(0.5)  # gentle on the holder-analysis RPC

    return candidates


async def _collect_from_peer_discovery(session, existing: set[str], sample_size: int = 25) -> dict:
    """
    Cheap graph expansion: sample some already-active elite/core wallets
    and look at who they've recently transacted with. Counter-parties
    that recur across multiple elite wallets are promising candidates —
    smart traders often cluster (shared alpha groups, copy-trading,
    following the same deployers).
    """
    candidates = {}

    res = await session.execute(
        select(PremiumWallet)
        .where(PremiumWallet.status == "active", PremiumWallet.tier.in_(["elite", "core"]))
        .order_by(PremiumWallet.reputation_score.desc())
        .limit(sample_size)
    )
    seeds = res.scalars().all()
    if not seeds:
        return candidates

    counterparties = Counter()

    for wallet in seeds:
        try:
            events = await asyncio.wait_for(
                get_recent_signatures(wallet.wallet_address, limit=5), timeout=8
            )
        except Exception:
            continue

        for ev in events or []:
            for field in ("from", "to"):
                addr = (ev.get(field) or "").strip()
                if addr and addr != wallet.wallet_address:
                    counterparties[addr] += 1

        await asyncio.sleep(0.3)

    for addr, count in counterparties.items():
        if count >= 2 and addr not in existing:
            candidates[addr] = ("peer_discovery", f"linked to {count} elite wallets")

    return candidates


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------

async def _validate_candidate(address: str) -> dict | None:
    """
    Minimum bar for a candidate to even enter the database:
      - resolvable wallet with a non-trivial portfolio value, OR
      - real recent on-chain activity
    Cheap checks only — full scoring happens later, continuously, once
    the wallet is being monitored.
    """
    try:
        assets = await asyncio.wait_for(fetch_wallet_assets(address, max_assets=50), timeout=10)
    except Exception:
        assets = {"native_sol": 0.0, "tokens": []}

    try:
        recent = await asyncio.wait_for(get_recent_signatures(address, limit=5), timeout=8)
    except Exception:
        recent = []

    if not assets.get("tokens") and not recent:
        return None

    sol_price = 0.0
    try:
        sol_price = await asyncio.wait_for(get_sol_price_usd(), timeout=6)
    except Exception:
        pass

    native_value = (assets.get("native_sol") or 0.0) * sol_price
    token_value = sum((t.get("value") or 0.0) for t in assets.get("tokens") or [])
    portfolio_value = native_value + token_value

    if portfolio_value < PREMIUM_WALLET_MIN_PORTFOLIO_USD and not recent:
        return None

    return {"portfolio_value_usd": portfolio_value, "has_activity": bool(recent)}


# ---------------------------------------------------------------------
# Main discovery pass
# ---------------------------------------------------------------------

async def run_discovery_cycle() -> dict:
    """
    One full discovery pass: gather candidates from every source, cap
    to the configured batch size, validate, and insert new
    status="candidate" rows. Safe to call repeatedly (idempotent —
    de-dupes against existing wallets every time).
    """
    stats = {"sources": {}, "validated": 0, "inserted": 0, "skipped_capacity": 0}

    async with async_session() as session:
        existing = await _existing_addresses(session)
        current_count = await _current_wallet_count(session)

    if current_count >= PREMIUM_WALLET_HARD_CAP:
        logger.info(
            f"Discovery skipped: wallet DB at hard cap ({current_count}/{PREMIUM_WALLET_HARD_CAP})"
        )
        stats["skipped_capacity"] = current_count
        return stats

    room = max(0, PREMIUM_WALLET_HARD_CAP - current_count)
    budget = min(PREMIUM_DISCOVERY_BATCH_SIZE, room)
    if budget <= 0:
        return stats

    all_candidates: dict = {}

    async with async_session() as session:
        try:
            found = await _collect_from_kol_wallets(session, existing)
        except Exception as e:
            logger.warning(f"Discovery source kol_wallets failed: {e}")
            found = {}
        stats["sources"]["kol_wallets"] = len(found)
        all_candidates.update(found)

        try:
            found = await _collect_from_tracked_wallets(session, existing)
        except Exception as e:
            logger.warning(f"Discovery source tracked_wallet_popularity failed: {e}")
            found = {}
        stats["sources"]["tracked_wallet_popularity"] = len(found)
        all_candidates.update(found)

        # These hit external RPCs repeatedly — only run if we still have
        # budget left after the cheap DB-only sources above.
        #
        # independent_pump_discovery runs FIRST among them and is the
        # engine's primary source (Blueprint Problem 3): it discovers
        # tokens straight from the public Pump.fun feed and monitors
        # their own trading activity directly, with zero dependency on
        # models.signal_token.SignalToken. winning_signal_holder right
        # after it is a secondary, opportunistic bonus on top — not a
        # dependency this engine waits on or requires.
        if len(all_candidates) < budget:
            try:
                found = await _collect_from_independent_pump_discovery(
                    session, existing | set(all_candidates)
                )
            except Exception as e:
                logger.warning(f"Discovery source independent_pump_discovery failed: {e}")
                found = {}
            stats["sources"]["independent_pump_discovery"] = len(found)
            all_candidates.update(found)

        if len(all_candidates) < budget:
            try:
                found = await _collect_from_winning_signals(session, existing | set(all_candidates))
            except Exception as e:
                logger.warning(f"Discovery source winning_signal_holder failed: {e}")
                found = {}
            stats["sources"]["winning_signal_holder"] = len(found)
            all_candidates.update(found)

        if len(all_candidates) < budget:
            try:
                found = await _collect_from_peer_discovery(session, existing | set(all_candidates))
            except Exception as e:
                logger.warning(f"Discovery source peer_discovery failed: {e}")
                found = {}
            stats["sources"]["peer_discovery"] = len(found)
            all_candidates.update(found)

    items = list(all_candidates.items())[:budget]

    to_insert = []
    for address, (source, detail) in items:
        validation = await _validate_candidate(address)
        if not validation:
            continue
        stats["validated"] += 1
        to_insert.append(
            PremiumWallet(
                wallet_address=address,
                source=source,
                source_detail=detail[:250] if detail else None,
                status="candidate",
                tier="candidate",
                reputation_score=0.0,
                wallet_value_usd=validation["portfolio_value_usd"],
                last_wallet_snapshot_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        await asyncio.sleep(0.15)

    if to_insert:
        async with async_session() as session:
            session.add_all(to_insert)
            await session.commit()
        stats["inserted"] = len(to_insert)
        logger.info(
            f"🔎 Discovery cycle: {len(items)} candidates seen, "
            f"{stats['validated']} validated, {stats['inserted']} inserted "
            f"(DB size now ~{current_count + stats['inserted']})"
        )
    else:
        logger.info(f"🔎 Discovery cycle: {len(items)} candidates seen, none passed validation")

    return stats


async def premium_discovery_loop(interval_seconds: int) -> None:
    """
    Runs Bootstrap Discovery and Continuous Discovery as one seamless
    loop: while the number of *active* (qualified) wallets is still
    below the Minimum Operational Threshold, it cycles at the faster
    PREMIUM_BOOTSTRAP_DISCOVERY_INTERVAL_SECONDS cadence to populate the
    database as quickly as the underlying APIs comfortably allow; once
    that threshold is reached it automatically settles into the normal
    steady-state `interval_seconds` cadence. No manual phase switch, no
    restart required — each cycle re-checks and picks its own pace.
    """
    logger.info("🧠 Premium Smart Wallet Discovery Engine active (Bootstrap -> Continuous)")
    was_bootstrapping = None
    while True:
        try:
            await run_discovery_cycle()
        except Exception as e:
            logger.error(f"Premium discovery cycle error: {e}")

        try:
            async with async_session() as session:
                active_count = await _active_wallet_count(session)
        except Exception as e:
            logger.warning(f"Discovery loop could not read active wallet count (non-fatal): {e}")
            active_count = None

        if active_count is not None and active_count < PREMIUM_WALLET_INITIAL_TARGET:
            sleep_for = PREMIUM_BOOTSTRAP_DISCOVERY_INTERVAL_SECONDS
            if was_bootstrapping is not True:
                logger.info(
                    f"🚀 Bootstrap Discovery: {active_count}/{PREMIUM_WALLET_INITIAL_TARGET} active "
                    f"wallets — cycling every {sleep_for}s until the Minimum Operational Threshold is met"
                )
            was_bootstrapping = True
        else:
            sleep_for = interval_seconds
            if was_bootstrapping is True:
                logger.info(
                    f"✅ Minimum Operational Threshold reached ({active_count} active wallets) — "
                    f"Discovery settling into steady-state Continuous Discovery every {sleep_for}s"
                )
            was_bootstrapping = False

        await asyncio.sleep(sleep_for)
