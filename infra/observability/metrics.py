"""
infra/observability/metrics.py

NEW in v4 (Bible §10 — Observability Stack Design). The audit found zero
metrics/tracing exposed anywhere — but also found that `multi_rpc_manager`
already has real internal instrumentation (`provider_stats()`,
`queue_depths()`), it just was never surfaced outside the process (only
reachable internally, e.g. via the `/premium_stats` command).

This module is additive, not new engineering on top of
`multi_rpc_manager`: it reads those two existing methods on a timer and
republishes them as Prometheus gauges. If the `prometheus_client` package
isn't installed (as in this sandbox), `configure_metrics()` degrades to a
no-op with a warning rather than failing the whole process — metrics are
observability, not a hard dependency the app should crash without.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("AlphaPulse.Metrics")

_metrics_available = False

try:
    from prometheus_client import Gauge, start_http_server

    _metrics_available = True

    PROVIDER_TOTAL_REQUESTS = Gauge(
        "alphapulse_provider_requests_total", "Total requests per provider", ["provider"]
    )
    PROVIDER_SUCCESS_RATE = Gauge(
        "alphapulse_provider_success_rate_pct", "Success rate % per provider", ["provider"]
    )
    PROVIDER_AVG_LATENCY_MS = Gauge(
        "alphapulse_provider_avg_latency_ms", "Average latency (ms) per provider", ["provider"]
    )
    PROVIDER_CIRCUIT_BROKEN = Gauge(
        "alphapulse_provider_circuit_broken", "1 if circuit breaker is open for this provider", ["provider"]
    )
    QUEUE_DEPTH = Gauge(
        "alphapulse_rpc_queue_depth", "Queued (not yet dispatched) RPC jobs per priority", ["priority"]
    )
except ImportError:
    pass


async def _poll_loop(interval_seconds: int) -> None:
    from providers.rpc.multi_rpc_manager import multi_rpc_manager

    while True:
        try:
            for provider_name, stats in multi_rpc_manager.provider_stats().items():
                PROVIDER_TOTAL_REQUESTS.labels(provider=provider_name).set(stats["total_requests"])
                PROVIDER_SUCCESS_RATE.labels(provider=provider_name).set(stats["success_rate_pct"])
                PROVIDER_AVG_LATENCY_MS.labels(provider=provider_name).set(stats["average_latency_ms"])
                PROVIDER_CIRCUIT_BROKEN.labels(provider=provider_name).set(
                    1 if stats["circuit_broken"] else 0
                )
            for priority, depth in multi_rpc_manager.queue_depths().items():
                QUEUE_DEPTH.labels(priority=str(priority)).set(depth)
        except Exception:
            logger.exception("Metrics poll cycle failed (non-fatal)")

        await asyncio.sleep(interval_seconds)


def configure_metrics(port: int = 9090, poll_interval_seconds: int = 15) -> None:
    """
    Call once per process (Bot Gateway, each Worker) at startup. Starts a
    `/metrics` HTTP endpoint on `port` and a background task that
    republishes multi_rpc_manager's existing stats every
    `poll_interval_seconds`. No-ops with a warning if prometheus_client
    isn't installed — see module docstring.
    """
    if not _metrics_available:
        logger.warning(
            "prometheus_client is not installed; metrics endpoint disabled. "
            "Install it (pip install prometheus-client) to enable "
            "/metrics scraping."
        )
        return

    start_http_server(port)
    logger.info("Metrics endpoint listening on :%d/metrics", port)
    asyncio.create_task(_poll_loop(poll_interval_seconds))
