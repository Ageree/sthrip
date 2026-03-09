"""Tests for failed authentication rate limiting (Task 20)."""
import os
import contextlib
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from contextlib import contextmanager

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, AgentTier,
    RateLimitTier, PrivacyLevel, HubRoute, FeeCollection
)
from sthrip.services.rate_limiter import RateLimiter, RateLimitExceeded

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
]


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    return engine


@pytest.fixture
def db_session_factory(db_engine):
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def client_with_real_limiter(db_engine, db_session_factory):
    """Client that uses a real (local-fallback) rate limiter for auth tests."""

    @contextmanager
    def get_test_db():
        session = db_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # Use a real RateLimiter with forced local fallback (no Redis dependency)
    real_limiter = RateLimiter.__new__(RateLimiter)
    real_limiter.default_tier = RateLimiter.__init__.__defaults__[1] if RateLimiter.__init__.__defaults__ else None
    from sthrip.services.rate_limiter import RateLimitTier
    real_limiter.default_tier = RateLimitTier.STANDARD
    real_limiter._local_cache = {}
    real_limiter._cache_lock = __import__("threading").Lock()
    real_limiter.use_redis = False
    real_limiter.redis = None

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-03-03T00:00:00",
        "checks": {},
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger"}))

        # Database patches
        for mod in [
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
        ]:
            stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))

        stack.enter_context(patch("sthrip.db.database.create_tables"))

        # Rate limiter patches
        stack.enter_context(patch("sthrip.services.rate_limiter.get_rate_limiter", return_value=real_limiter))
        for mod in ["api.main_v2", "api.deps", "api.routers.agents"]:
            stack.enter_context(patch(f"{mod}.get_rate_limiter", return_value=real_limiter))

        # Audit log patches
        stack.enter_context(patch("sthrip.services.audit_logger.log_event"))
        for mod in [
            "api.main_v2",
            "api.deps",
            "api.routers.agents",
            "api.routers.payments",
            "api.routers.balance",
            "api.routers.admin",
        ]:
            stack.enter_context(patch(f"{mod}.audit_log"))

        # Monitoring & webhook patches
        stack.enter_context(patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor))
        stack.enter_context(patch("sthrip.services.monitoring.setup_default_monitoring", return_value=mock_monitor))
        stack.enter_context(patch("sthrip.services.webhook_service.get_webhook_service", return_value=mock_webhook))
        stack.enter_context(patch("sthrip.services.webhook_service.queue_webhook"))

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


class TestFailedAuthRateLimiting:
    """Test that failed auth attempts are rate limited per IP."""

    def test_single_failed_auth_returns_401(self, client_with_real_limiter):
        """A single failed auth attempt should return 401, not 429."""
        r = client_with_real_limiter.get(
            "/v2/me",
            headers={"Authorization": "Bearer invalid_key"},
        )
        assert r.status_code == 401

    def test_rate_limit_after_many_failed_attempts(self, client_with_real_limiter):
        """After 20 failed auth attempts from same IP, should return 429."""
        for i in range(20):
            r = client_with_real_limiter.get(
                "/v2/me",
                headers={"Authorization": "Bearer invalid_key"},
            )
            assert r.status_code == 401, f"Attempt {i+1} should be 401"

        # 21st attempt should be rate limited
        r = client_with_real_limiter.get(
            "/v2/me",
            headers={"Authorization": "Bearer invalid_key"},
        )
        assert r.status_code == 429

    def test_successful_auth_not_counted(self, client_with_real_limiter):
        """Successful auth should not increment the failed counter."""
        # Register an agent first
        r = client_with_real_limiter.post("/v2/agents/register", json={
            "agent_name": "auth-test-agent",
            "xmr_address": "test_addr_123",
        })
        assert r.status_code == 201
        valid_key = r.json()["api_key"]

        # Make several successful requests
        for _ in range(25):
            r = client_with_real_limiter.get(
                "/v2/me",
                headers={"Authorization": f"Bearer {valid_key}"},
            )
            assert r.status_code == 200

        # Should still work (not rate limited)
        r = client_with_real_limiter.get(
            "/v2/me",
            headers={"Authorization": f"Bearer {valid_key}"},
        )
        assert r.status_code == 200

    def test_missing_auth_counted_as_failed(self, client_with_real_limiter):
        """Missing auth header should also count toward failed attempts."""
        for i in range(20):
            r = client_with_real_limiter.get("/v2/me")
            assert r.status_code == 401

        r = client_with_real_limiter.get("/v2/me")
        assert r.status_code == 429
