"""
Prometheus metrics for Sthrip API.

Requires: pip install prometheus-client
Falls back to no-op if not installed.
"""

import time
from typing import Optional

try:
    from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

    http_requests_total = Counter(
        "http_requests_total",
        "Total HTTP requests",
        ["method", "endpoint", "status"],
    )
    http_request_duration = Histogram(
        "http_request_duration_seconds",
        "HTTP request latency",
        ["method", "endpoint"],
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    )
    hub_payments_total = Counter(
        "hub_payments_total",
        "Hub routing payments created",
        ["status"],
    )
    balance_ops_total = Counter(
        "balance_operations_total",
        "Balance operations (deposit/withdraw)",
        ["operation"],
    )

    PROMETHEUS_AVAILABLE = True

except ImportError:
    PROMETHEUS_AVAILABLE = False

    # No-op stubs
    class _Noop:
        def labels(self, *a, **kw):
            return self
        def inc(self, *a, **kw):
            pass
        def observe(self, *a, **kw):
            pass

    http_requests_total = _Noop()
    http_request_duration = _Noop()
    hub_payments_total = _Noop()
    balance_ops_total = _Noop()


def get_metrics_response() -> Optional[tuple]:
    """Return (body_bytes, content_type) or None if prometheus not available."""
    if not PROMETHEUS_AVAILABLE:
        return None
    return generate_latest(), CONTENT_TYPE_LATEST
