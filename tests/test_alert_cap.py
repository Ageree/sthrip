"""Tests that alert list has a maximum size."""
from sthrip.services.monitoring import HealthMonitor, AlertSeverity


def test_alerts_capped_at_max():
    """Alert list must not grow beyond max size."""
    monitor = HealthMonitor()
    max_alerts = 1000  # Expected cap
    for i in range(max_alerts + 100):
        monitor._create_alert(
            AlertSeverity.INFO, f"Test {i}", f"Message {i}", "test"
        )
    assert len(monitor._alerts) <= max_alerts
