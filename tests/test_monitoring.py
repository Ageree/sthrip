"""Tests for health monitoring and alerting service"""
import time
import threading
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from sthrip.config import get_settings
from sthrip.services.monitoring import (
    HealthCheck,
    Alert,
    AlertSeverity,
    AlertManager,
    HealthMonitor,
    setup_default_monitoring,
    get_monitor,
    dispatch_alert_webhook,
)


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass construction
# ─────────────────────────────────────────────────────────────────────────────


class TestHealthCheckDataclass:
    def test_creation_with_defaults(self):
        fn = lambda: {"healthy": True}
        hc = HealthCheck(name="test", check_fn=fn)
        assert hc.name == "test"
        assert hc.interval_seconds == 60
        assert hc.timeout_seconds == 10
        assert hc.last_check is None
        assert hc.last_result is None
        assert hc.failures == 0
        assert hc.max_failures == 3

    def test_creation_with_custom_values(self):
        fn = lambda: {"healthy": True}
        hc = HealthCheck(
            name="db",
            check_fn=fn,
            interval_seconds=30,
            timeout_seconds=5,
            max_failures=5,
        )
        assert hc.interval_seconds == 30
        assert hc.timeout_seconds == 5
        assert hc.max_failures == 5


class TestAlertDataclass:
    def test_creation_with_defaults(self):
        ts = datetime.now(timezone.utc)
        alert = Alert(
            id="a1",
            severity=AlertSeverity.WARNING,
            title="Disk full",
            message="disk > 90%",
            source="system",
            timestamp=ts,
        )
        assert alert.id == "a1"
        assert alert.severity == AlertSeverity.WARNING
        assert alert.acknowledged is False
        assert alert.resolved is False

    def test_severity_values(self):
        assert AlertSeverity.INFO.value == "info"
        assert AlertSeverity.WARNING.value == "warning"
        assert AlertSeverity.CRITICAL.value == "critical"


# ─────────────────────────────────────────────────────────────────────────────
# HealthMonitor
# ─────────────────────────────────────────────────────────────────────────────


class TestHealthMonitorInit:
    def test_initial_state(self):
        m = HealthMonitor()
        assert m.checks == {}
        assert m.running is False
        assert m._alerts == []
        assert m._alert_handlers == []


class TestRegisterCheck:
    def test_adds_check(self):
        m = HealthMonitor()
        hc = HealthCheck(name="db", check_fn=lambda: {"healthy": True})
        m.register_check(hc)
        assert "db" in m.checks
        assert m.checks["db"] is hc

    def test_overwrites_existing_check(self):
        m = HealthMonitor()
        hc1 = HealthCheck(name="db", check_fn=lambda: {"healthy": True})
        hc2 = HealthCheck(name="db", check_fn=lambda: {"healthy": False})
        m.register_check(hc1)
        m.register_check(hc2)
        assert m.checks["db"] is hc2


class TestRunSingleCheck:
    def test_healthy_check_records_result(self):
        m = HealthMonitor()
        fn = MagicMock(return_value={"healthy": True, "details": "ok"})
        hc = HealthCheck(name="db", check_fn=fn)
        m.register_check(hc)

        result = m._run_check(hc)

        fn.assert_called_once()
        assert result["healthy"] is True
        assert result["check_name"] == "db"
        assert "timestamp" in result
        assert hc.last_result is result
        assert hc.last_check is not None
        assert hc.failures == 0

    def test_unhealthy_check_increments_failures(self):
        m = HealthMonitor()
        fn = MagicMock(return_value={"healthy": False, "error": "timeout"})
        hc = HealthCheck(name="db", check_fn=fn, max_failures=5)
        m.register_check(hc)

        m._run_check(hc)
        assert hc.failures == 1
        m._run_check(hc)
        assert hc.failures == 2

    def test_healthy_resets_failures(self):
        m = HealthMonitor()
        fn = MagicMock(return_value={"healthy": True})
        hc = HealthCheck(name="db", check_fn=fn)
        hc.failures = 3
        m.register_check(hc)

        m._run_check(hc)
        assert hc.failures == 0

    def test_exception_in_check_fn(self):
        m = HealthMonitor()
        fn = MagicMock(side_effect=RuntimeError("boom"))
        hc = HealthCheck(name="db", check_fn=fn)
        m.register_check(hc)

        result = m._run_check(hc)
        assert result["healthy"] is False
        assert "boom" in result["error"]
        assert hc.failures == 1

    def test_alert_created_on_max_failures(self):
        m = HealthMonitor()
        fn = MagicMock(return_value={"healthy": False})
        hc = HealthCheck(name="db", check_fn=fn, max_failures=3)
        m.register_check(hc)

        for _ in range(3):
            m._run_check(hc)

        assert len(m._alerts) == 1
        assert m._alerts[0].severity == AlertSeverity.WARNING
        assert "db" in m._alerts[0].title

    def test_critical_alert_at_five_failures(self):
        m = HealthMonitor()
        fn = MagicMock(return_value={"healthy": False})
        hc = HealthCheck(name="db", check_fn=fn, max_failures=3)
        m.register_check(hc)

        for _ in range(5):
            m._run_check(hc)

        critical_alerts = [a for a in m._alerts if a.severity == AlertSeverity.CRITICAL]
        assert len(critical_alerts) >= 1


class TestGetHealthReport:
    def test_all_healthy(self):
        m = HealthMonitor()
        fn = MagicMock(return_value={"healthy": True})
        m.register_check(HealthCheck(name="a", check_fn=fn))
        m.register_check(HealthCheck(name="b", check_fn=fn))
        m.run_all_checks()

        report = m.get_health_report()
        assert report["status"] == "healthy"
        assert report["checks_total"] == 2
        assert report["checks_healthy"] == 2

    def test_degraded(self):
        m = HealthMonitor()
        m.register_check(HealthCheck(name="a", check_fn=lambda: {"healthy": True}))
        m.register_check(HealthCheck(name="b", check_fn=lambda: {"healthy": False}))
        m.run_all_checks()

        report = m.get_health_report()
        assert report["status"] == "degraded"
        assert report["checks_healthy"] == 1

    def test_unhealthy(self):
        m = HealthMonitor()
        m.register_check(HealthCheck(name="a", check_fn=lambda: {"healthy": False}))
        m.register_check(HealthCheck(name="b", check_fn=lambda: {"healthy": False}))
        m.run_all_checks()

        report = m.get_health_report()
        assert report["status"] == "unhealthy"
        assert report["checks_healthy"] == 0

    def test_no_checks_run_yet(self):
        m = HealthMonitor()
        m.register_check(HealthCheck(name="a", check_fn=lambda: {"healthy": True}))
        report = m.get_health_report()
        # No last_result yet → healthy_count=0, total=1 → unhealthy
        assert report["status"] == "unhealthy"
        assert report["checks"] == {}


class TestGetAlerts:
    def _make_monitor_with_alerts(self):
        m = HealthMonitor()
        ts = datetime.now(timezone.utc)
        m._alerts = [
            Alert(id="1", severity=AlertSeverity.INFO, title="t", message="m",
                  source="s", timestamp=ts),
            Alert(id="2", severity=AlertSeverity.WARNING, title="t", message="m",
                  source="s", timestamp=ts + timedelta(seconds=1)),
            Alert(id="3", severity=AlertSeverity.CRITICAL, title="t", message="m",
                  source="s", timestamp=ts + timedelta(seconds=2), acknowledged=True),
        ]
        return m

    def test_returns_all_alerts(self):
        m = self._make_monitor_with_alerts()
        alerts = m.get_alerts()
        assert len(alerts) == 3

    def test_filter_by_severity(self):
        m = self._make_monitor_with_alerts()
        alerts = m.get_alerts(severity=AlertSeverity.WARNING)
        assert len(alerts) == 1
        assert alerts[0].id == "2"

    def test_unacknowledged_only(self):
        m = self._make_monitor_with_alerts()
        alerts = m.get_alerts(unacknowledged_only=True)
        assert len(alerts) == 2
        assert all(not a.acknowledged for a in alerts)

    def test_sorted_newest_first(self):
        m = self._make_monitor_with_alerts()
        alerts = m.get_alerts()
        assert alerts[0].id == "3"


class TestAcknowledgeAndResolve:
    def test_acknowledge_existing(self):
        m = HealthMonitor()
        m._alerts = [
            Alert(id="x", severity=AlertSeverity.INFO, title="t", message="m",
                  source="s", timestamp=datetime.now(timezone.utc)),
        ]
        assert m.acknowledge_alert("x") is True
        assert m._alerts[0].acknowledged is True

    def test_acknowledge_missing(self):
        m = HealthMonitor()
        assert m.acknowledge_alert("nope") is False

    def test_resolve_existing(self):
        m = HealthMonitor()
        m._alerts = [
            Alert(id="y", severity=AlertSeverity.INFO, title="t", message="m",
                  source="s", timestamp=datetime.now(timezone.utc)),
        ]
        assert m.resolve_alert("y") is True
        assert m._alerts[0].resolved is True

    def test_resolve_missing(self):
        m = HealthMonitor()
        assert m.resolve_alert("nope") is False


class TestCheckIntervalLogic:
    def test_skips_check_if_interval_not_elapsed(self):
        m = HealthMonitor()
        fn = MagicMock(return_value={"healthy": True})
        hc = HealthCheck(name="db", check_fn=fn, interval_seconds=300)
        hc.last_check = datetime.now(timezone.utc)
        m.register_check(hc)

        # The _monitor_loop logic checks interval — simulate it
        for check in m.checks.values():
            if check.last_check is None or \
               (datetime.now(timezone.utc) - check.last_check).total_seconds() >= check.interval_seconds:
                m._run_check(check)

        fn.assert_not_called()

    def test_runs_check_when_interval_elapsed(self):
        m = HealthMonitor()
        fn = MagicMock(return_value={"healthy": True})
        hc = HealthCheck(name="db", check_fn=fn, interval_seconds=1)
        hc.last_check = datetime.now(timezone.utc) - timedelta(seconds=5)
        m.register_check(hc)

        for check in m.checks.values():
            if check.last_check is None or \
               (datetime.now(timezone.utc) - check.last_check).total_seconds() >= check.interval_seconds:
                m._run_check(check)

        fn.assert_called_once()

    def test_runs_check_when_never_checked(self):
        m = HealthMonitor()
        fn = MagicMock(return_value={"healthy": True})
        hc = HealthCheck(name="db", check_fn=fn, interval_seconds=300)
        m.register_check(hc)

        for check in m.checks.values():
            if check.last_check is None or \
               (datetime.now(timezone.utc) - check.last_check).total_seconds() >= check.interval_seconds:
                m._run_check(check)

        fn.assert_called_once()


class TestStartStopMonitoring:
    def test_start_sets_running(self):
        m = HealthMonitor()
        m.register_check(HealthCheck(name="db", check_fn=lambda: {"healthy": True}))

        m.start_monitoring()
        try:
            assert m.running is True
            assert m._thread is not None
            assert m._thread.is_alive()
        finally:
            m.stop_monitoring()

    def test_stop_clears_running(self):
        m = HealthMonitor()
        m.register_check(HealthCheck(name="db", check_fn=lambda: {"healthy": True}))
        m.start_monitoring()
        m.stop_monitoring()

        assert m.running is False

    def test_double_start_is_noop(self):
        m = HealthMonitor()
        m.register_check(HealthCheck(name="db", check_fn=lambda: {"healthy": True}))
        m.start_monitoring()
        thread1 = m._thread
        m.start_monitoring()  # should not create a second thread
        assert m._thread is thread1
        m.stop_monitoring()


class TestThreadSafety:
    """Thread safety: concurrent _run_check + get_health_report must not corrupt state."""

    def test_concurrent_run_check_and_get_health_report(self):
        m = HealthMonitor()
        call_count = 0

        def toggling_check():
            nonlocal call_count
            call_count += 1
            return {"healthy": call_count % 2 == 0}

        hc = HealthCheck(name="toggle", check_fn=toggling_check, max_failures=1)
        m.register_check(hc)

        errors = []
        stop_event = threading.Event()
        iterations = 200

        def writer():
            """Background thread running _run_check in a loop."""
            for _ in range(iterations):
                try:
                    m._run_check(hc)
                except Exception as exc:
                    errors.append(exc)
                if stop_event.is_set():
                    break

        def reader():
            """Main-like thread calling get_health_report concurrently."""
            for _ in range(iterations):
                try:
                    report = m.get_health_report()
                    # Validate report structure is not corrupted
                    assert "status" in report
                    assert "checks_total" in report
                    assert isinstance(report["checks_total"], int)
                    assert report["checks_total"] >= 0
                    assert isinstance(report.get("unacknowledged_alerts", 0), int)
                except Exception as exc:
                    errors.append(exc)
                if stop_event.is_set():
                    break

        writer_thread = threading.Thread(target=writer)
        reader_thread = threading.Thread(target=reader)

        writer_thread.start()
        reader_thread.start()

        writer_thread.join(timeout=10)
        reader_thread.join(timeout=10)
        stop_event.set()

        assert errors == [], f"Thread safety errors: {errors}"
        # Verify state is consistent after concurrent access
        report = m.get_health_report()
        assert report["checks_total"] == 1
        assert report["status"] in ("healthy", "degraded", "unhealthy")


class TestAlertManager:
    def test_register_and_dispatch(self):
        am = AlertManager()
        handler = MagicMock()
        am.register_channel("email", handler)

        alert = Alert(
            id="1", severity=AlertSeverity.INFO, title="t", message="m",
            source="s", timestamp=datetime.now(timezone.utc),
        )
        am.dispatch(alert)
        handler.assert_called_once_with(alert)

    def test_dispatch_continues_on_handler_error(self):
        am = AlertManager()
        bad = MagicMock(side_effect=RuntimeError("fail"))
        good = MagicMock()
        am.register_channel("bad", bad)
        am.register_channel("good", good)

        alert = Alert(
            id="1", severity=AlertSeverity.INFO, title="t", message="m",
            source="s", timestamp=datetime.now(timezone.utc),
        )
        am.dispatch(alert)
        good.assert_called_once_with(alert)


class TestAlertHandlerDispatch:
    def test_alert_handler_called_on_failure(self):
        m = HealthMonitor()
        handler = MagicMock()
        m.register_alert_handler(handler)

        fn = MagicMock(return_value={"healthy": False})
        hc = HealthCheck(name="db", check_fn=fn, max_failures=1)
        m.register_check(hc)
        m._run_check(hc)

        handler.assert_called_once()
        alert_arg = handler.call_args[0][0]
        assert isinstance(alert_arg, Alert)

    def test_handler_exception_does_not_propagate(self):
        m = HealthMonitor()
        handler = MagicMock(side_effect=RuntimeError("boom"))
        m.register_alert_handler(handler)

        fn = MagicMock(return_value={"healthy": False})
        hc = HealthCheck(name="db", check_fn=fn, max_failures=1)
        m.register_check(hc)
        # Should not raise
        m._run_check(hc)


class TestSetupDefaultMonitoring:
    @patch("sthrip.services.monitoring._monitor", None)
    @patch("sthrip.services.monitoring.create_database_health_check")
    @patch("sthrip.services.monitoring.create_redis_health_check")
    @patch("sthrip.services.monitoring.create_system_health_check")
    @patch("sthrip.services.monitoring.create_wallet_health_check")
    def test_registers_default_checks(self, mock_wallet, mock_system, mock_redis, mock_db):
        mock_db.return_value = HealthCheck(name="database", check_fn=lambda: {})
        mock_redis.return_value = HealthCheck(name="redis", check_fn=lambda: {})
        mock_system.return_value = HealthCheck(name="system_resources", check_fn=lambda: {})

        monitor = setup_default_monitoring(include_wallet=False)
        assert "database" in monitor.checks
        assert "redis" in monitor.checks
        assert "system_resources" in monitor.checks
        assert "wallet_rpc" not in monitor.checks
        mock_wallet.assert_not_called()

    @patch("sthrip.services.monitoring._monitor", None)
    @patch("sthrip.services.monitoring.create_database_health_check")
    @patch("sthrip.services.monitoring.create_redis_health_check")
    @patch("sthrip.services.monitoring.create_system_health_check")
    @patch("sthrip.services.monitoring.create_wallet_health_check")
    def test_includes_wallet_when_requested(self, mock_wallet, mock_system, mock_redis, mock_db):
        mock_db.return_value = HealthCheck(name="database", check_fn=lambda: {})
        mock_redis.return_value = HealthCheck(name="redis", check_fn=lambda: {})
        mock_system.return_value = HealthCheck(name="system_resources", check_fn=lambda: {})
        mock_wallet.return_value = HealthCheck(name="wallet_rpc", check_fn=lambda: {})

        monitor = setup_default_monitoring(include_wallet=True)
        assert "wallet_rpc" in monitor.checks

    @patch("sthrip.services.monitoring._monitor", None)
    @patch("sthrip.services.monitoring.create_database_health_check")
    @patch("sthrip.services.monitoring.create_redis_health_check")
    @patch("sthrip.services.monitoring.create_system_health_check")
    @patch.dict("os.environ", {"ALERT_WEBHOOK_URL": "https://example.com/hook"})
    def test_registers_webhook_handler_when_url_set(self, mock_system, mock_redis, mock_db):
        mock_db.return_value = HealthCheck(name="database", check_fn=lambda: {})
        mock_redis.return_value = HealthCheck(name="redis", check_fn=lambda: {})
        mock_system.return_value = HealthCheck(name="system_resources", check_fn=lambda: {})

        monitor = setup_default_monitoring()
        assert len(monitor._alert_handlers) >= 1


class TestRunAllChecks:
    def test_runs_all_registered_checks(self):
        m = HealthMonitor()
        fn_a = MagicMock(return_value={"healthy": True})
        fn_b = MagicMock(return_value={"healthy": False})
        m.register_check(HealthCheck(name="a", check_fn=fn_a))
        m.register_check(HealthCheck(name="b", check_fn=fn_b))

        results = m.run_all_checks()
        fn_a.assert_called_once()
        fn_b.assert_called_once()
        assert "a" in results
        assert "b" in results
        assert results["a"]["healthy"] is True
        assert results["b"]["healthy"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Built-in health check factories
# ─────────────────────────────────────────────────────────────────────────────


class TestCreateDatabaseHealthCheck:
    @patch("sthrip.services.monitoring.get_engine" if False else "sthrip.db.database.get_engine")
    def test_healthy_database(self, mock_get_engine):
        """Database check returns healthy when SELECT 1 succeeds."""
        from sthrip.services.monitoring import create_database_health_check

        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_engine.return_value = mock_engine

        hc = create_database_health_check()
        assert hc.name == "database"
        assert hc.interval_seconds == 30

        result = hc.check_fn()
        assert result["healthy"] is True
        assert "Database connection OK" in result["details"]

    def test_unhealthy_database(self):
        """Database check returns unhealthy on exception."""
        from sthrip.services.monitoring import create_database_health_check

        hc = create_database_health_check()
        # The inner check() imports get_engine which may fail -- patch it
        with patch("sthrip.db.database.get_engine", side_effect=RuntimeError("no db")):
            result = hc.check_fn()
        assert result["healthy"] is False
        assert "no db" in result["error"]


class TestCreateRedisHealthCheck:
    def test_healthy_redis_with_redis(self):
        """Redis check returns healthy when ping succeeds."""
        from sthrip.services.monitoring import create_redis_health_check

        mock_limiter = MagicMock()
        mock_limiter.use_redis = True
        mock_limiter.redis.ping.return_value = True
        mock_limiter.redis.info.return_value = {"redis_version": "7.0.0"}

        with patch("sthrip.services.rate_limiter.get_rate_limiter", return_value=mock_limiter):
            hc = create_redis_health_check()
            result = hc.check_fn()

        assert result["healthy"] is True
        assert "7.0.0" in result["details"]

    def test_healthy_redis_no_redis(self):
        """Redis check returns healthy when using local cache."""
        from sthrip.services.monitoring import create_redis_health_check

        mock_limiter = MagicMock()
        mock_limiter.use_redis = False

        with patch("sthrip.services.rate_limiter.get_rate_limiter", return_value=mock_limiter):
            hc = create_redis_health_check()
            result = hc.check_fn()

        assert result["healthy"] is True
        assert "local cache" in result["details"]

    def test_unhealthy_redis(self):
        """Redis check returns unhealthy on exception."""
        from sthrip.services.monitoring import create_redis_health_check

        with patch("sthrip.services.rate_limiter.get_rate_limiter", side_effect=RuntimeError("conn refused")):
            hc = create_redis_health_check()
            result = hc.check_fn()

        assert result["healthy"] is False
        assert "conn refused" in result["error"]


class TestCreateWalletHealthCheck:
    def test_healthy_wallet(self):
        """Wallet check returns healthy when get_height succeeds."""
        from sthrip.services.monitoring import create_wallet_health_check

        mock_wallet = MagicMock()
        mock_wallet.get_height.return_value = 3000000

        with patch("sthrip.wallet.MoneroWalletRPC.from_env", return_value=mock_wallet):
            hc = create_wallet_health_check()
            result = hc.check_fn()

        assert result["healthy"] is True
        assert "3000000" in result["details"]

    def test_unhealthy_wallet(self):
        """Wallet check returns unhealthy on exception."""
        from sthrip.services.monitoring import create_wallet_health_check

        mock_wallet = MagicMock()
        mock_wallet.get_height.side_effect = ConnectionError("rpc down")

        with patch("sthrip.wallet.MoneroWalletRPC.from_env", return_value=mock_wallet):
            hc = create_wallet_health_check()
            result = hc.check_fn()

        assert result["healthy"] is False
        assert "rpc down" in result["error"]

    def test_wallet_client_created_once_reused_across_invocations(self):
        """from_env must be called exactly once at factory time, not on every check."""
        from sthrip.services.monitoring import create_wallet_health_check

        mock_wallet = MagicMock()
        mock_wallet.get_height.return_value = 1234567

        with patch("sthrip.wallet.MoneroWalletRPC.from_env", return_value=mock_wallet) as mock_from_env:
            hc = create_wallet_health_check()
            # Invoke the check function multiple times to simulate repeated polling
            hc.check_fn()
            hc.check_fn()
            hc.check_fn()

        # from_env must have been called exactly once (at factory creation time)
        mock_from_env.assert_called_once()
        # get_height must have been called once per check invocation
        assert mock_wallet.get_height.call_count == 3


class TestCreateSystemHealthCheck:
    @patch("sthrip.services.monitoring.PSUTIL_AVAILABLE", False)
    def test_psutil_not_available(self):
        """System check returns healthy when psutil not installed."""
        from sthrip.services.monitoring import create_system_health_check

        hc = create_system_health_check()
        result = hc.check_fn()
        assert result["healthy"] is True
        assert "psutil not installed" in result["details"]

    @patch("sthrip.services.monitoring.PSUTIL_AVAILABLE", True)
    def test_all_resources_healthy(self):
        """System check returns healthy when all resources are below thresholds."""
        import sthrip.services.monitoring as mod

        mock_psutil = MagicMock()
        mock_mem = MagicMock()
        mock_mem.percent = 50.0
        mock_psutil.virtual_memory.return_value = mock_mem

        mock_disk = MagicMock()
        mock_disk.percent = 40.0
        mock_psutil.disk_usage.return_value = mock_disk

        mock_psutil.cpu_percent.return_value = 30.0

        original = getattr(mod, "psutil", None)
        mod.psutil = mock_psutil
        try:
            hc = mod.create_system_health_check()
            result = hc.check_fn()
        finally:
            if original is not None:
                mod.psutil = original

        assert result["healthy"] is True
        assert result["details"]["memory_percent"] == 50.0
        assert result["details"]["disk_percent"] == 40.0
        assert result["details"]["cpu_percent"] == 30.0

    @patch("sthrip.services.monitoring.PSUTIL_AVAILABLE", True)
    def test_high_memory_unhealthy(self):
        """System check returns unhealthy when memory exceeds 90%."""
        import sthrip.services.monitoring as mod

        mock_psutil = MagicMock()
        mock_mem = MagicMock()
        mock_mem.percent = 95.0
        mock_psutil.virtual_memory.return_value = mock_mem

        mock_disk = MagicMock()
        mock_disk.percent = 40.0
        mock_psutil.disk_usage.return_value = mock_disk

        mock_psutil.cpu_percent.return_value = 30.0

        original = getattr(mod, "psutil", None)
        mod.psutil = mock_psutil
        try:
            hc = mod.create_system_health_check()
            result = hc.check_fn()
        finally:
            if original is not None:
                mod.psutil = original

        assert result["healthy"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Alert webhook dispatch
# ─────────────────────────────────────────────────────────────────────────────


class TestDispatchAlertWebhook:
    def _make_alert(self, source="test_src", severity=AlertSeverity.WARNING):
        return Alert(
            id="alert_1",
            severity=severity,
            title="Test Alert",
            message="Something happened",
            source=source,
            timestamp=datetime.now(timezone.utc),
        )

    def test_no_webhook_url_returns_early(self):
        """dispatch_alert_webhook is a no-op when ALERT_WEBHOOK_URL not set."""
        import sthrip.services.monitoring as mod
        mod._last_dispatch.clear()
        mod._validated_webhook_url = None

        with patch.dict("os.environ", {"ADMIN_API_KEY": "test-key", "ENVIRONMENT": "dev", "DATABASE_URL": "sqlite:///:memory:"}, clear=True):
            get_settings.cache_clear()
            # Should not raise
            dispatch_alert_webhook(self._make_alert())

    @patch.dict("os.environ", {"ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/123"})
    def test_discord_webhook_dispatch(self):
        """dispatch_alert_webhook sends Discord embed when URL is not Telegram."""
        get_settings.cache_clear()
        import sthrip.services.monitoring as mod
        mod._last_dispatch.clear()
        mod._validated_webhook_url = None

        mock_requests = MagicMock()
        with patch.dict("sys.modules", {"requests": mock_requests}):
            alert = self._make_alert(source="discord_test_src")
            dispatch_alert_webhook(alert)

        mock_requests.post.assert_called_once()
        call_kwargs = mock_requests.post.call_args
        body = call_kwargs[1].get("json") or call_kwargs[0][1]
        assert "embeds" in body

    @patch.dict("os.environ", {"ALERT_WEBHOOK_URL": "https://api.telegram.org/bot123/sendMessage?chat_id=456"})
    def test_telegram_webhook_dispatch(self):
        """dispatch_alert_webhook sends Telegram text when URL contains api.telegram.org."""
        get_settings.cache_clear()
        import sthrip.services.monitoring as mod
        mod._last_dispatch.clear()
        mod._validated_webhook_url = None

        mock_requests = MagicMock()
        with patch.dict("sys.modules", {"requests": mock_requests}):
            alert = self._make_alert(source="telegram_test_src")
            dispatch_alert_webhook(alert)

        mock_requests.post.assert_called_once()
        call_kwargs = mock_requests.post.call_args
        body = call_kwargs[1].get("json") or call_kwargs[0][1]
        assert "text" in body
        assert "parse_mode" in body

    @patch.dict("os.environ", {"ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/123"})
    def test_debounce_prevents_duplicate(self):
        """dispatch_alert_webhook debounces repeated alerts from same source."""
        get_settings.cache_clear()
        import sthrip.services.monitoring as mod
        mod._last_dispatch.clear()
        mod._validated_webhook_url = None

        mock_requests = MagicMock()
        with patch.dict("sys.modules", {"requests": mock_requests}):
            alert = self._make_alert(source="debounce_src")
            dispatch_alert_webhook(alert)
            assert mock_requests.post.call_count == 1

            # Second call within 5 minutes should be debounced
            dispatch_alert_webhook(alert)
            assert mock_requests.post.call_count == 1

    @patch.dict("os.environ", {"ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/123"})
    def test_request_exception_does_not_propagate(self):
        """dispatch_alert_webhook logs but doesn't raise on request failure."""
        import sthrip.services.monitoring as mod
        mod._last_dispatch.clear()
        mod._validated_webhook_url = None

        mock_requests = MagicMock()
        mock_requests.post.side_effect = RuntimeError("network error")
        with patch.dict("sys.modules", {"requests": mock_requests}):
            alert = self._make_alert(source="error_test_src")
            # Should not raise
            dispatch_alert_webhook(alert)

    @patch.dict("os.environ", {"ALERT_WEBHOOK_URL": "http://169.254.169.254/latest/meta-data/"})
    def test_ssrf_blocked_url_rejected(self):
        """dispatch_alert_webhook must reject SSRF-unsafe URLs."""
        get_settings.cache_clear()
        import sthrip.services.monitoring as mod
        mod._last_dispatch.clear()
        mod._validated_webhook_url = None

        mock_requests = MagicMock()
        with patch.dict("sys.modules", {"requests": mock_requests}):
            alert = self._make_alert(source="ssrf_test_src")
            dispatch_alert_webhook(alert)
        # Should NOT have posted — URL blocked by SSRF validation
        mock_requests.post.assert_not_called()
