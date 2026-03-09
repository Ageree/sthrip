"""Tests for the Admin Dashboard UI (Phase 1).

TDD: Write tests FIRST, then implement.
Tests cover:
- Login page and cookie-based auth
- Overview page with stats
- Agents list with search/filter
- Agent detail page
- Transactions list
- Balances page
- Deposits page
- Auth middleware (cookie check, auto-logout)
"""

import os
import time
import pytest
from decimal import Decimal
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from contextlib import ExitStack, contextmanager

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, AgentTier,
    RateLimitTier, PrivacyLevel, HubRoute, HubRouteStatus, FeeCollection,
)

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
]

ADMIN_KEY = "test-admin-key-for-dashboard"


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
def seed_data(db_session_factory):
    """Seed test data: 3 agents with balances and a payment."""
    session = db_session_factory()
    agents = []
    for i, (name, tier) in enumerate([
        ("agent-alpha", AgentTier.VERIFIED),
        ("agent-beta", AgentTier.FREE),
        ("agent-gamma", AgentTier.PREMIUM),
    ]):
        agent = Agent(
            agent_name=name,
            api_key_hash=f"hash_{i}",
            tier=tier,
            rate_limit_tier=RateLimitTier.STANDARD,
            privacy_level=PrivacyLevel.MEDIUM,
            is_active=True,
        )
        session.add(agent)
        session.flush()

        balance = AgentBalance(
            agent_id=agent.id,
            available=Decimal("10.0") * (i + 1),
            pending=Decimal("0"),
        )
        session.add(balance)

        rep = AgentReputation(
            agent_id=agent.id,
            trust_score=80 + i * 5,
            successful_transactions=i * 10,
            failed_transactions=i,
        )
        session.add(rep)
        agents.append(agent)

    # Add a hub route payment
    import secrets
    route = HubRoute(
        payment_id=secrets.token_hex(32),
        from_agent_id=agents[0].id,
        to_agent_id=agents[1].id,
        amount=Decimal("1.5"),
        fee_amount=Decimal("0.015"),
        status=HubRouteStatus.CONFIRMED,
    )
    session.add(route)
    session.commit()
    session.close()
    return agents


@pytest.fixture
def admin_client(db_engine, db_session_factory, seed_data):
    """Test client with admin UI endpoints."""

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

    mock_limiter = MagicMock()
    mock_limiter.check_rate_limit.return_value = None

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy", "timestamp": "2026-03-09T00:00:00", "checks": {}
    }
    mock_monitor.get_alerts.return_value = []
    mock_monitor.start_monitoring.return_value = None
    mock_monitor.stop_monitoring.return_value = None

    mock_webhook = MagicMock()
    mock_webhook.start_worker = MagicMock(return_value=MagicMock())
    mock_webhook.stop_worker.return_value = None
    mock_webhook.close = MagicMock(return_value=MagicMock())
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    mock_registry = MagicMock()
    mock_registry.get_stats.return_value = {
        "total_agents": 3,
        "by_tier": {"free": 1, "verified": 1, "premium": 1},
        "active_24h": 2,
    }

    mock_collector = MagicMock()
    mock_collector.get_revenue_stats.return_value = {
        "total_fees": 0.5, "period_days": 30
    }

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "HUB_MODE": "ledger",
            "DATABASE_URL": "sqlite:///:memory:",
            "ENVIRONMENT": "dev",
            "ADMIN_API_KEY": ADMIN_KEY,
        }))
        # Patch get_db in all modules that import it
        for mod in [
            "sthrip.db.database",
            "api.deps",
            "api.routers.health",
            "api.routers.agents",
            "api.routers.payments",
            "api.routers.balance",
            "api.routers.webhooks",
            "sthrip.services.agent_registry",
            "sthrip.services.fee_collector",
        ]:
            try:
                stack.enter_context(patch(f"{mod}.get_db", get_test_db))
            except AttributeError:
                pass
        # Admin UI views module
        try:
            stack.enter_context(patch("api.admin_ui.views.get_db", get_test_db))
        except (AttributeError, ModuleNotFoundError):
            pass

        stack.enter_context(patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor))
        stack.enter_context(patch("sthrip.services.monitoring.setup_default_monitoring", return_value=mock_monitor))
        stack.enter_context(patch("sthrip.services.webhook_service.get_webhook_service", return_value=mock_webhook))
        stack.enter_context(patch("sthrip.services.rate_limiter.get_rate_limiter", return_value=mock_limiter))
        stack.enter_context(patch("sthrip.db.database.create_tables"))
        stack.enter_context(patch("sthrip.db.database.get_engine", return_value=MagicMock()))

        for mod in [
            "api.deps",
            "api.routers.agents",
            "api.routers.payments",
            "api.routers.balance",
            "api.routers.admin",
        ]:
            try:
                stack.enter_context(patch(f"{mod}.audit_log"))
            except AttributeError:
                pass

        import importlib
        import api.main_v2 as main_mod
        importlib.reload(main_mod)

        app = main_mod.create_app()
        client = TestClient(app, raise_server_exceptions=False)
        yield client


def _login(client: TestClient) -> TestClient:
    """Helper to log in and return the client with session cookie."""
    resp = client.post("/admin/login", data={"admin_key": ADMIN_KEY})
    # Should redirect to /admin/
    assert resp.status_code in (200, 302, 303), f"Login failed: {resp.status_code}"
    return client


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdminAuth:
    """Admin dashboard authentication tests."""

    def test_login_page_returns_200(self, admin_client):
        """GET /admin/login should show login form."""
        resp = admin_client.get("/admin/login")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_login_page_has_form(self, admin_client):
        """Login page should have a form with admin_key input."""
        resp = admin_client.get("/admin/login")
        assert "admin_key" in resp.text
        assert "<form" in resp.text.lower()

    def test_login_with_valid_key(self, admin_client):
        """POST /admin/login with valid key sets session cookie."""
        resp = admin_client.post(
            "/admin/login",
            data={"admin_key": ADMIN_KEY},
            follow_redirects=False,
        )
        # Should redirect to /admin/
        assert resp.status_code in (302, 303)
        assert "/admin" in resp.headers.get("location", "")
        # Should set a session cookie
        cookies = resp.cookies
        assert "admin_session" in dict(cookies) or any(
            "admin_session" in str(c) for c in resp.headers.get_list("set-cookie")
        )

    def test_login_with_invalid_key(self, admin_client):
        """POST /admin/login with invalid key shows error."""
        resp = admin_client.post(
            "/admin/login",
            data={"admin_key": "wrong-key"},
        )
        # Should stay on login page with error
        assert resp.status_code in (200, 401)
        assert "invalid" in resp.text.lower() or "error" in resp.text.lower()

    def test_unauthenticated_redirects_to_login(self, admin_client):
        """GET /admin/ without session should redirect to login."""
        resp = admin_client.get("/admin/", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "login" in resp.headers.get("location", "")

    def test_logout(self, admin_client):
        """GET /admin/logout should clear session and redirect to login."""
        _login(admin_client)
        resp = admin_client.get("/admin/logout", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "login" in resp.headers.get("location", "")


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestOverviewPage:
    """Admin overview page tests."""

    def test_overview_shows_stats(self, admin_client):
        """GET /admin/ should show overview with stats."""
        _login(admin_client)
        resp = admin_client.get("/admin/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_overview_contains_agent_count(self, admin_client):
        """Overview should show total agent count."""
        _login(admin_client)
        resp = admin_client.get("/admin/")
        # Should mention agents somewhere
        assert "agent" in resp.text.lower()

    def test_overview_has_navigation(self, admin_client):
        """Overview should have nav links to other admin pages."""
        _login(admin_client)
        resp = admin_client.get("/admin/")
        assert "/admin/agents" in resp.text
        assert "/admin/transactions" in resp.text


class TestAgentsListPage:
    """Admin agents list page tests."""

    def test_agents_page_returns_200(self, admin_client):
        """GET /admin/agents should return HTML with agents list."""
        _login(admin_client)
        resp = admin_client.get("/admin/agents")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_agents_page_shows_agents(self, admin_client):
        """Agents page should list seeded agents."""
        _login(admin_client)
        resp = admin_client.get("/admin/agents")
        assert "agent-alpha" in resp.text
        assert "agent-beta" in resp.text

    def test_agents_page_search(self, admin_client):
        """GET /admin/agents?search=alpha should filter results."""
        _login(admin_client)
        resp = admin_client.get("/admin/agents?search=alpha")
        assert resp.status_code == 200
        assert "agent-alpha" in resp.text

    def test_agents_page_filter_by_tier(self, admin_client):
        """GET /admin/agents?tier=free should filter by tier."""
        _login(admin_client)
        resp = admin_client.get("/admin/agents?tier=free")
        assert resp.status_code == 200
        assert "agent-beta" in resp.text


class TestAgentDetailPage:
    """Admin agent detail page tests."""

    def test_agent_detail_returns_200(self, admin_client, seed_data):
        """GET /admin/agents/{id} should show agent details."""
        _login(admin_client)
        agent_id = seed_data[0].id
        resp = admin_client.get(f"/admin/agents/{agent_id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_agent_detail_shows_name(self, admin_client, seed_data):
        """Agent detail should show agent name."""
        _login(admin_client)
        agent_id = seed_data[0].id
        resp = admin_client.get(f"/admin/agents/{agent_id}")
        assert "agent-alpha" in resp.text

    def test_agent_detail_shows_balance(self, admin_client, seed_data):
        """Agent detail should show balance info."""
        _login(admin_client)
        agent_id = seed_data[0].id
        resp = admin_client.get(f"/admin/agents/{agent_id}")
        assert "10" in resp.text  # balance is 10.0

    def test_agent_detail_not_found(self, admin_client):
        """GET /admin/agents/999 should return 404."""
        _login(admin_client)
        resp = admin_client.get("/admin/agents/99999")
        assert resp.status_code == 404


class TestTransactionsPage:
    """Admin transactions page tests."""

    def test_transactions_page_returns_200(self, admin_client):
        """GET /admin/transactions should return HTML."""
        _login(admin_client)
        resp = admin_client.get("/admin/transactions")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_transactions_page_shows_payments(self, admin_client):
        """Transactions page should list seeded payments."""
        _login(admin_client)
        resp = admin_client.get("/admin/transactions")
        # Should show the completed payment
        assert "1.5" in resp.text or "completed" in resp.text.lower()


class TestBalancesPage:
    """Admin balances page tests."""

    def test_balances_page_returns_200(self, admin_client):
        """GET /admin/balances should return HTML."""
        _login(admin_client)
        resp = admin_client.get("/admin/balances")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_balances_page_shows_amounts(self, admin_client):
        """Balances page should show agent balances."""
        _login(admin_client)
        resp = admin_client.get("/admin/balances")
        # At least one balance should be visible
        assert "10" in resp.text  # first agent has 10.0
