"""Tests that Prometheus metric labels match actual usage."""

import pytest


def test_hub_payments_total_labels():
    """hub_payments_total must accept (status, tier) labels."""
    from sthrip.services.metrics import PROMETHEUS_AVAILABLE
    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus-client not installed")
    from sthrip.services.metrics import hub_payments_total
    # Should not raise ValueError
    hub_payments_total.labels(status="completed", tier="standard").inc()


def test_balance_ops_total_labels():
    """balance_ops_total must accept (operation, token) labels."""
    from sthrip.services.metrics import PROMETHEUS_AVAILABLE
    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus-client not installed")
    from sthrip.services.metrics import balance_ops_total
    # Should not raise ValueError
    balance_ops_total.labels(operation="deposit", token="XMR").inc()
