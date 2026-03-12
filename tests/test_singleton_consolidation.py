"""Tests for singleton consolidation into app.state + Depends() DI.

RED phase: These tests are written BEFORE the implementation.
They verify:
  1. app.state is populated with service instances after lifespan startup
  2. DI providers in api/deps.py pull from request.app.state
  3. Module-level get_*() functions still work (backward compatibility)
  4. No TODO(I1) comments remain in the codebase after the refactor
"""

import os
import contextlib
import threading
import pytest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch, AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    HubRoute, FeeCollection, PendingWithdrawal, Transaction,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_TEST_ENCRYPTION_KEY = "uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE="

_COMMON_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    PendingWithdrawal.__table__,
    Transaction.__table__,
]


@pytest.fixture
def _db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_COMMON_TABLES)
    return engine


@pytest.fixture
def _db_session_factory(_db_engine):
    return sessionmaker(bind=_db_engine, expire_on_commit=False)


def _make_get_test_db(session_factory):
    @contextmanager
    def get_test_db():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    return get_test_db


_GET_DB_MODULES = [
    "sthrip.db.database",
    "sthrip.services.agent_registry",
    "sthrip.services.fee_collector",
    "sthrip.services.webhook_service",
    "api.main_v2",
    "api.deps",
    "api.routers.health",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.webhooks",
]

_AUDIT_LOG_MODULES = [
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.admin",
]

_RATE_LIMITER_MODULES = [
    "sthrip.services.rate_limiter",
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
]


def _make_mock_limiter():
    mock = MagicMock()
    mock.check_rate_limit.return_value = None
    mock.check_ip_rate_limit.return_value = None
    mock.get_limit_status.return_value = {"requests_remaining": 100}
    mock.use_redis = False
    return mock


def _make_mock_monitor():
    mock = MagicMock()
    mock.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-03-03T00:00:00",
        "checks": {},
    }
    mock.get_alerts.return_value = []
    return mock


def _make_mock_webhook():
    mock = MagicMock()
    mock.get_delivery_stats.return_value = {"total": 0}
    mock.start_worker = AsyncMock(return_value=None)
    mock.stop_worker = MagicMock()
    mock.close = AsyncMock()
    return mock


@pytest.fixture
def _patched_client(_db_engine, _db_session_factory):
    """FastAPI TestClient with common mocks, yields (client, app) tuple.

    Uses TestClient as a context manager so that the lifespan (and therefore
    app.state population) runs reliably before the first assert.
    """
    get_test_db = _make_get_test_db(_db_session_factory)
    mock_limiter = _make_mock_limiter()
    mock_monitor = _make_mock_monitor()
    mock_webhook = _make_mock_webhook()

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger"}))

        for mod in _GET_DB_MODULES:
            stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))
        stack.enter_context(patch("sthrip.db.database.create_tables"))

        for mod in _RATE_LIMITER_MODULES:
            stack.enter_context(
                patch(f"{mod}.get_rate_limiter", return_value=mock_limiter)
            )

        for mod in _AUDIT_LOG_MODULES:
            stack.enter_context(patch(f"{mod}.audit_log"))

        stack.enter_context(
            patch(
                "sthrip.services.monitoring.get_monitor",
                return_value=mock_monitor,
            )
        )
        stack.enter_context(
            patch(
                "sthrip.services.monitoring.setup_default_monitoring",
                return_value=mock_monitor,
            )
        )
        stack.enter_context(
            patch(
                "sthrip.services.webhook_service.get_webhook_service",
                return_value=mock_webhook,
            )
        )
        stack.enter_context(patch("sthrip.services.webhook_service.queue_webhook"))

        from api.main_v2 import app
        # Use TestClient as a context manager so the ASGI lifespan (startup /
        # shutdown events) executes reliably before the tests run.  Without
        # this, Starlette triggers lifespan lazily on the first request, which
        # can race with the app.state assertions below.
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, app


# ===========================================================================
# GROUP 1: app.state populated during lifespan
# ===========================================================================

class TestAppStatePopulatedDuringLifespan:
    """After lifespan startup, app.state must hold all service instances."""

    def test_app_state_has_rate_limiter(self, _patched_client):
        """app.state.rate_limiter must be set after lifespan startup."""
        client, app = _patched_client
        # Trigger lifespan by making a request
        client.get("/health")
        assert hasattr(app.state, "rate_limiter"), (
            "app.state.rate_limiter not set — lifespan must assign it"
        )

    def test_app_state_has_monitor(self, _patched_client):
        """app.state.monitor must be set after lifespan startup."""
        client, app = _patched_client
        client.get("/health")
        assert hasattr(app.state, "monitor"), (
            "app.state.monitor not set — lifespan must assign it"
        )

    def test_app_state_has_webhook_service(self, _patched_client):
        """app.state.webhook_service must be set after lifespan startup."""
        client, app = _patched_client
        client.get("/health")
        assert hasattr(app.state, "webhook_service"), (
            "app.state.webhook_service not set — lifespan must assign it"
        )

    def test_app_state_has_fee_collector(self, _patched_client):
        """app.state.fee_collector must be set after lifespan startup."""
        client, app = _patched_client
        client.get("/health")
        assert hasattr(app.state, "fee_collector"), (
            "app.state.fee_collector not set — lifespan must assign it"
        )

    def test_app_state_has_agent_registry(self, _patched_client):
        """app.state.agent_registry must be set after lifespan startup."""
        client, app = _patched_client
        client.get("/health")
        assert hasattr(app.state, "agent_registry"), (
            "app.state.agent_registry not set — lifespan must assign it"
        )

    def test_app_state_has_idempotency_store(self, _patched_client):
        """app.state.idempotency_store must be set after lifespan startup."""
        client, app = _patched_client
        client.get("/health")
        assert hasattr(app.state, "idempotency_store"), (
            "app.state.idempotency_store not set — lifespan must assign it"
        )

    def test_app_state_instances_are_correct_types(self, _patched_client):
        """app.state services must be non-None after lifespan startup.

        In the test environment services are mocked, so we only assert they
        are present (not None). A separate unit test verifies the exact type
        when the real constructors are used.
        """
        client, app = _patched_client

        assert app.state.rate_limiter is not None, "app.state.rate_limiter is None"
        assert app.state.fee_collector is not None, "app.state.fee_collector is None"
        assert app.state.agent_registry is not None, "app.state.agent_registry is None"
        assert app.state.idempotency_store is not None, "app.state.idempotency_store is None"
        assert app.state.webhook_service is not None, "app.state.webhook_service is None"
        assert app.state.monitor is not None, "app.state.monitor is None"

    def test_app_state_real_service_types(self):
        """_populate_app_state stores real class instances when not mocked."""
        from sthrip.services.rate_limiter import RateLimiter
        from sthrip.services.fee_collector import FeeCollector
        from sthrip.services.agent_registry import AgentRegistry
        from sthrip.services.idempotency import IdempotencyStore
        from sthrip.services.webhook_service import WebhookService
        from sthrip.services.monitoring import HealthMonitor
        from api.main_v2 import _populate_app_state, FastAPI

        mock_monitor = MagicMock(spec=HealthMonitor)
        mock_webhook = MagicMock(spec=WebhookService)
        services = {"monitor": mock_monitor, "webhook_service": mock_webhook}

        dummy_app = FastAPI()
        _populate_app_state(dummy_app, services)

        assert isinstance(dummy_app.state.fee_collector, FeeCollector)
        assert isinstance(dummy_app.state.agent_registry, AgentRegistry)
        assert isinstance(dummy_app.state.idempotency_store, IdempotencyStore)
        assert isinstance(dummy_app.state.rate_limiter, RateLimiter)
        assert dummy_app.state.monitor is mock_monitor
        assert dummy_app.state.webhook_service is mock_webhook

    def test_app_state_services_are_same_objects_across_requests(self, _patched_client):
        """The same service instance must be returned across multiple requests."""
        client, app = _patched_client
        client.get("/health")

        # Read the instance once
        rate_limiter_id = id(app.state.rate_limiter)

        # Make another request and verify identity is preserved
        client.get("/health")
        assert id(app.state.rate_limiter) == rate_limiter_id, (
            "app.state.rate_limiter must be the same object across requests"
        )


# ===========================================================================
# GROUP 2: DI providers in api/deps.py pull from request.app.state
# ===========================================================================

class TestDepsProvidersReadFromAppState:
    """DI providers added in api/deps.py must read from request.app.state."""

    def test_get_rate_limiter_dep_exists_in_deps(self):
        """api.deps must expose a get_rate_limiter_dep callable."""
        import api.deps as deps
        assert hasattr(deps, "get_rate_limiter_dep"), (
            "api.deps.get_rate_limiter_dep not found — add it"
        )
        assert callable(deps.get_rate_limiter_dep)

    def test_get_monitor_dep_exists_in_deps(self):
        """api.deps must expose a get_monitor_dep callable."""
        import api.deps as deps
        assert hasattr(deps, "get_monitor_dep"), (
            "api.deps.get_monitor_dep not found — add it"
        )
        assert callable(deps.get_monitor_dep)

    def test_get_webhook_service_dep_exists_in_deps(self):
        """api.deps must expose a get_webhook_service_dep callable."""
        import api.deps as deps
        assert hasattr(deps, "get_webhook_service_dep"), (
            "api.deps.get_webhook_service_dep not found — add it"
        )
        assert callable(deps.get_webhook_service_dep)

    def test_get_fee_collector_dep_exists_in_deps(self):
        """api.deps must expose a get_fee_collector_dep callable."""
        import api.deps as deps
        assert hasattr(deps, "get_fee_collector_dep"), (
            "api.deps.get_fee_collector_dep not found — add it"
        )
        assert callable(deps.get_fee_collector_dep)

    def test_get_agent_registry_dep_exists_in_deps(self):
        """api.deps must expose a get_agent_registry_dep callable."""
        import api.deps as deps
        assert hasattr(deps, "get_agent_registry_dep"), (
            "api.deps.get_agent_registry_dep not found — add it"
        )
        assert callable(deps.get_agent_registry_dep)

    def test_get_idempotency_store_dep_exists_in_deps(self):
        """api.deps must expose a get_idempotency_store_dep callable."""
        import api.deps as deps
        assert hasattr(deps, "get_idempotency_store_dep"), (
            "api.deps.get_idempotency_store_dep not found — add it"
        )
        assert callable(deps.get_idempotency_store_dep)

    def test_rate_limiter_dep_returns_app_state_instance(self):
        """get_rate_limiter_dep must return the instance stored on app.state."""
        from api.deps import get_rate_limiter_dep
        from sthrip.services.rate_limiter import RateLimiter

        mock_limiter = MagicMock(spec=RateLimiter)
        mock_request = MagicMock()
        mock_request.app.state.rate_limiter = mock_limiter

        result = get_rate_limiter_dep(mock_request)
        assert result is mock_limiter

    def test_monitor_dep_returns_app_state_instance(self):
        """get_monitor_dep must return the instance stored on app.state."""
        from api.deps import get_monitor_dep
        from sthrip.services.monitoring import HealthMonitor

        mock_monitor = MagicMock(spec=HealthMonitor)
        mock_request = MagicMock()
        mock_request.app.state.monitor = mock_monitor

        result = get_monitor_dep(mock_request)
        assert result is mock_monitor

    def test_webhook_service_dep_returns_app_state_instance(self):
        """get_webhook_service_dep must return the instance stored on app.state."""
        from api.deps import get_webhook_service_dep
        from sthrip.services.webhook_service import WebhookService

        mock_svc = MagicMock(spec=WebhookService)
        mock_request = MagicMock()
        mock_request.app.state.webhook_service = mock_svc

        result = get_webhook_service_dep(mock_request)
        assert result is mock_svc

    def test_fee_collector_dep_returns_app_state_instance(self):
        """get_fee_collector_dep must return the instance stored on app.state."""
        from api.deps import get_fee_collector_dep
        from sthrip.services.fee_collector import FeeCollector

        mock_fc = MagicMock(spec=FeeCollector)
        mock_request = MagicMock()
        mock_request.app.state.fee_collector = mock_fc

        result = get_fee_collector_dep(mock_request)
        assert result is mock_fc

    def test_agent_registry_dep_returns_app_state_instance(self):
        """get_agent_registry_dep must return the instance stored on app.state."""
        from api.deps import get_agent_registry_dep
        from sthrip.services.agent_registry import AgentRegistry

        mock_reg = MagicMock(spec=AgentRegistry)
        mock_request = MagicMock()
        mock_request.app.state.agent_registry = mock_reg

        result = get_agent_registry_dep(mock_request)
        assert result is mock_reg

    def test_idempotency_store_dep_returns_app_state_instance(self):
        """get_idempotency_store_dep must return the instance stored on app.state."""
        from api.deps import get_idempotency_store_dep
        from sthrip.services.idempotency import IdempotencyStore

        mock_store = MagicMock(spec=IdempotencyStore)
        mock_request = MagicMock()
        mock_request.app.state.idempotency_store = mock_store

        result = get_idempotency_store_dep(mock_request)
        assert result is mock_store


# ===========================================================================
# GROUP 3: Backward compatibility — module-level get_*() still work
# ===========================================================================

class TestModuleLevelGetterBackwardCompat:
    """The module-level get_*() functions must remain callable and return
    a valid instance. They are used by background tasks and CLI code that
    have no access to FastAPI's request context.
    """

    def test_get_rate_limiter_returns_instance(self):
        """get_rate_limiter() must return a RateLimiter (or raise on Redis miss)."""
        from sthrip.services.rate_limiter import get_rate_limiter, RateLimiter
        with patch("sthrip.services.rate_limiter.RateLimiter") as MockRL:
            instance = MagicMock(spec=RateLimiter)
            MockRL.return_value = instance
            # Reset the module-level singleton so the mock is used
            import sthrip.services.rate_limiter as rl_mod
            original = rl_mod._limiter
            rl_mod._limiter = None
            try:
                result = get_rate_limiter()
                assert result is instance
            finally:
                rl_mod._limiter = original

    def test_get_fee_collector_returns_instance(self):
        """get_fee_collector() must return a FeeCollector."""
        from sthrip.services.fee_collector import get_fee_collector, FeeCollector
        import sthrip.services.fee_collector as fc_mod
        original = fc_mod._collector
        fc_mod._collector = None
        try:
            result = get_fee_collector()
            assert isinstance(result, FeeCollector)
        finally:
            fc_mod._collector = original

    def test_get_webhook_service_returns_instance(self):
        """get_webhook_service() must return a WebhookService."""
        from sthrip.services.webhook_service import get_webhook_service, WebhookService
        import sthrip.services.webhook_service as ws_mod
        original = ws_mod._service
        ws_mod._service = None
        try:
            result = get_webhook_service()
            assert isinstance(result, WebhookService)
        finally:
            ws_mod._service = original

    def test_get_registry_returns_instance(self):
        """get_registry() must return an AgentRegistry."""
        from sthrip.services.agent_registry import get_registry, AgentRegistry
        import sthrip.services.agent_registry as ar_mod
        original = ar_mod._registry
        ar_mod._registry = None
        try:
            result = get_registry()
            assert isinstance(result, AgentRegistry)
        finally:
            ar_mod._registry = original

    def test_get_monitor_returns_instance(self):
        """get_monitor() must return a HealthMonitor."""
        from sthrip.services.monitoring import get_monitor, HealthMonitor
        import sthrip.services.monitoring as mon_mod
        original = mon_mod._monitor
        mon_mod._monitor = None
        try:
            result = get_monitor()
            assert isinstance(result, HealthMonitor)
        finally:
            mon_mod._monitor = original

    def test_get_idempotency_store_returns_instance(self):
        """get_idempotency_store() must return an IdempotencyStore."""
        from sthrip.services.idempotency import get_idempotency_store, IdempotencyStore
        import sthrip.services.idempotency as idem_mod
        original = idem_mod._store
        idem_mod._store = None
        try:
            result = get_idempotency_store()
            assert isinstance(result, IdempotencyStore)
        finally:
            idem_mod._store = original

    def test_module_level_getters_are_thread_safe(self):
        """Module-level getters must be thread-safe (double-checked locking)."""
        from sthrip.services.fee_collector import get_fee_collector, FeeCollector
        import sthrip.services.fee_collector as fc_mod

        original = fc_mod._collector
        fc_mod._collector = None
        instances = []
        errors = []

        def _get():
            try:
                instances.append(get_fee_collector())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        fc_mod._collector = original

        assert not errors, f"Thread safety errors: {errors}"
        # All threads must have received the same instance
        assert len(set(id(i) for i in instances)) == 1, (
            "Multiple FeeCollector instances created under concurrency"
        )


# ===========================================================================
# GROUP 4: No TODO(I1) comments remain after the refactor
# ===========================================================================

class TestNoTodoI1CommentsRemain:
    """After the refactor all TODO(I1) markers must be removed from source files."""

    _SOURCE_FILES = [
        "sthrip/services/rate_limiter.py",
        "sthrip/services/fee_collector.py",
        "sthrip/services/webhook_service.py",
        "sthrip/services/agent_registry.py",
        "sthrip/services/monitoring.py",
        "sthrip/services/idempotency.py",
        "sthrip/crypto.py",
        "api/helpers.py",
    ]

    def _read_file(self, relative_path: str) -> str:
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        return (root / relative_path).read_text()

    @pytest.mark.parametrize("path", _SOURCE_FILES)
    def test_no_todo_i1_in_source_file(self, path):
        """Each source file must not contain any TODO(I1) comment."""
        content = self._read_file(path)
        assert "TODO(I1)" not in content, (
            f"{path} still contains TODO(I1) — remove it after consolidation"
        )


# ===========================================================================
# GROUP 5: app.state DI providers are idempotent on second request
# ===========================================================================

class TestDepsProviderIdempotency:
    """DI providers must return the same object on repeated calls."""

    def test_rate_limiter_dep_idempotent(self):
        """Calling get_rate_limiter_dep twice with the same request yields the same instance."""
        from api.deps import get_rate_limiter_dep
        from sthrip.services.rate_limiter import RateLimiter

        mock_limiter = MagicMock(spec=RateLimiter)
        mock_request = MagicMock()
        mock_request.app.state.rate_limiter = mock_limiter

        first = get_rate_limiter_dep(mock_request)
        second = get_rate_limiter_dep(mock_request)
        assert first is second

    def test_monitor_dep_idempotent(self):
        """Calling get_monitor_dep twice with the same request yields the same instance."""
        from api.deps import get_monitor_dep
        from sthrip.services.monitoring import HealthMonitor

        mock_monitor = MagicMock(spec=HealthMonitor)
        mock_request = MagicMock()
        mock_request.app.state.monitor = mock_monitor

        assert get_monitor_dep(mock_request) is get_monitor_dep(mock_request)

    def test_webhook_service_dep_idempotent(self):
        """Calling get_webhook_service_dep twice yields the same instance."""
        from api.deps import get_webhook_service_dep
        from sthrip.services.webhook_service import WebhookService

        mock_svc = MagicMock(spec=WebhookService)
        mock_request = MagicMock()
        mock_request.app.state.webhook_service = mock_svc

        assert get_webhook_service_dep(mock_request) is get_webhook_service_dep(mock_request)


# ===========================================================================
# GROUP 6: Edge cases / error paths
# ===========================================================================

class TestEdgeCases:
    """Edge cases for the consolidation."""

    def test_dep_provider_raises_if_state_not_set(self):
        """DI providers must raise AttributeError (not silently return None) if
        app.state was never populated. This catches bugs where lifespan was skipped.
        """
        from api.deps import get_rate_limiter_dep

        empty_state = type("State", (), {})()  # state with no attributes
        mock_request = MagicMock()
        mock_request.app.state = empty_state

        with pytest.raises(AttributeError):
            get_rate_limiter_dep(mock_request)

    def test_dep_provider_raises_for_monitor_if_state_not_set(self):
        """get_monitor_dep raises AttributeError when app.state.monitor is absent."""
        from api.deps import get_monitor_dep

        empty_state = type("State", (), {})()
        mock_request = MagicMock()
        mock_request.app.state = empty_state

        with pytest.raises(AttributeError):
            get_monitor_dep(mock_request)

    def test_app_state_after_lifespan_shutdown_not_tested(self, _patched_client):
        """Smoke-test: after the TestClient context exits, no crash occurs."""
        # The _patched_client fixture handles lifespan enter/exit cleanly.
        client, app = _patched_client
        response = client.get("/health")
        assert response.status_code in (200, 503)  # Either healthy or degraded is fine
