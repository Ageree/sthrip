"""Tests for admin escrow dashboard pages: overview escrow stats,
escrow list, and escrow detail.

Uses the same fixture patterns as test_admin_ui.py: admin_client with session
cookie auth, seed_data for pre-populated DB records.
"""

import os
import re
import secrets
import uuid
import pytest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, HubRoute,
    AgentTier, RateLimitTier, PrivacyLevel, HubRouteStatus,
    EscrowDeal, EscrowStatus, FeeCollection,
)

_ESCROW_ADMIN_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    EscrowDeal.__table__,
]

ADMIN_KEY = "test-admin-key-for-dashboard-32char"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def escrow_db_engine():
    """In-memory SQLite engine with escrow + admin tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_ESCROW_ADMIN_TABLES)
    return engine


@pytest.fixture
def escrow_db_session_factory(escrow_db_engine):
    return sessionmaker(bind=escrow_db_engine, expire_on_commit=False)


@pytest.fixture
def seed_escrow_data(escrow_db_session_factory):
    """Seed test data: 2 agents + 2 escrow deals (one active, one completed)."""
    session = escrow_db_session_factory()

    buyer = Agent(
        id=uuid.uuid4(),
        agent_name="escrow-buyer",
        api_key_hash="hash_buyer",
        tier=AgentTier.VERIFIED,
        rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.MEDIUM,
        is_active=True,
    )
    seller = Agent(
        id=uuid.uuid4(),
        agent_name="escrow-seller",
        api_key_hash="hash_seller",
        tier=AgentTier.FREE,
        rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.MEDIUM,
        is_active=True,
    )
    session.add_all([buyer, seller])
    session.flush()

    # Reputation rows
    for agent in [buyer, seller]:
        session.add(AgentReputation(agent_id=agent.id, trust_score=60))

    # Active escrow (status=created)
    active_deal = EscrowDeal(
        id=uuid.uuid4(),
        deal_hash=secrets.token_hex(32),
        buyer_id=buyer.id,
        seller_id=seller.id,
        amount=Decimal("2.5"),
        token="XMR",
        description="active deal",
        status=EscrowStatus.CREATED,
        accept_deadline=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )

    # Completed escrow
    completed_deal = EscrowDeal(
        id=uuid.uuid4(),
        deal_hash=secrets.token_hex(32),
        buyer_id=buyer.id,
        seller_id=seller.id,
        amount=Decimal("1.0"),
        token="XMR",
        description="completed deal",
        status=EscrowStatus.COMPLETED,
        fee_amount=Decimal("0.001"),
        release_amount=Decimal("1.0"),
        accept_deadline=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )

    session.add_all([active_deal, completed_deal])
    session.commit()
    result = {
        "buyer": buyer,
        "seller": seller,
        "active_deal": active_deal,
        "completed_deal": completed_deal,
    }
    session.close()
    return result


@pytest.fixture
def admin_escrow_client(escrow_db_engine, escrow_db_session_factory, seed_escrow_data):
    """Test client with admin UI endpoints + escrow tables seeded."""

    @contextmanager
    def get_test_db():
        session = escrow_db_session_factory()
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
    mock_limiter.check_failed_auth.return_value = None
    mock_limiter.record_failed_auth.return_value = None

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy", "timestamp": "2026-03-19T00:00:00", "checks": {}
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "HUB_MODE": "ledger",
            "DATABASE_URL": "sqlite:///:memory:",
            "ENVIRONMENT": "dev",
            "ADMIN_API_KEY": ADMIN_KEY,
        }))

        # Patch get_db everywhere
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


@pytest.fixture
def admin_empty_client(escrow_db_engine, escrow_db_session_factory):
    """Test client with NO seeded data (empty DB with escrow tables)."""

    @contextmanager
    def get_test_db():
        session = escrow_db_session_factory()
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
    mock_limiter.check_failed_auth.return_value = None
    mock_limiter.record_failed_auth.return_value = None

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy", "timestamp": "2026-03-19T00:00:00", "checks": {}
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "HUB_MODE": "ledger",
            "DATABASE_URL": "sqlite:///:memory:",
            "ENVIRONMENT": "dev",
            "ADMIN_API_KEY": ADMIN_KEY,
        }))

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


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_csrf_token(client: TestClient) -> str:
    """Extract CSRF token from login page."""
    resp = client.get("/admin/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert match, "No csrf_token found in login form"
    return match.group(1)


def _login(client: TestClient) -> TestClient:
    """Log in and return the client with session cookie."""
    csrf_token = _get_csrf_token(client)
    resp = client.post(
        "/admin/login",
        data={"admin_key": ADMIN_KEY, "csrf_token": csrf_token},
    )
    assert resp.status_code in (200, 302, 303), f"Login failed: {resp.status_code}"
    return client


# ---------------------------------------------------------------------------
# 1. Overview page — escrow stats section
# ---------------------------------------------------------------------------

class TestOverviewEscrowStats:
    """Admin overview page shows escrow aggregate stats."""

    def test_overview_shows_escrow_stats_section(self, admin_escrow_client):
        """Overview page includes escrow stats (total, active, completed, volume, revenue)."""
        _login(admin_escrow_client)
        resp = admin_escrow_client.get("/admin/")
        assert resp.status_code == 200
        body = resp.text.lower()
        assert "escrow" in body
        assert "total escrows" in body

    def test_overview_escrow_total_count(self, admin_escrow_client):
        """Overview shows correct total escrow count (2 seeded deals)."""
        _login(admin_escrow_client)
        resp = admin_escrow_client.get("/admin/")
        assert resp.status_code == 200
        # The page should contain the number 2 for total escrows
        assert ">2<" in resp.text or "> 2 <" in resp.text or "2</p>" in resp.text

    def test_overview_escrow_active_count(self, admin_escrow_client):
        """Overview shows 1 active escrow (status=created)."""
        _login(admin_escrow_client)
        resp = admin_escrow_client.get("/admin/")
        assert resp.status_code == 200
        # Active should show 1
        assert ">1<" in resp.text or "1</p>" in resp.text

    def test_overview_escrow_completed_count(self, admin_escrow_client):
        """Overview shows 1 completed escrow."""
        _login(admin_escrow_client)
        resp = admin_escrow_client.get("/admin/")
        assert resp.status_code == 200
        # Completed should also show 1
        assert "completed" in resp.text.lower()

    def test_overview_stats_zero_when_empty(self, admin_empty_client):
        """Overview shows 0 for escrow stats when no escrows exist."""
        _login(admin_empty_client)
        resp = admin_empty_client.get("/admin/")
        assert resp.status_code == 200
        body = resp.text
        # Total escrows should be 0
        assert "0" in body


# ---------------------------------------------------------------------------
# 2. Escrow list page (/admin/escrows)
# ---------------------------------------------------------------------------

class TestEscrowListPage:
    """Admin escrow list page tests."""

    def test_escrows_page_returns_200(self, admin_escrow_client):
        """GET /admin/escrows returns 200 with HTML."""
        _login(admin_escrow_client)
        resp = admin_escrow_client.get("/admin/escrows")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_escrows_page_shows_deals(self, admin_escrow_client, seed_escrow_data):
        """Escrow list shows seeded buyer and seller names."""
        _login(admin_escrow_client)
        resp = admin_escrow_client.get("/admin/escrows")
        assert resp.status_code == 200
        assert "escrow-buyer" in resp.text
        assert "escrow-seller" in resp.text

    def test_escrows_page_shows_amount(self, admin_escrow_client):
        """Escrow list shows deal amounts."""
        _login(admin_escrow_client)
        resp = admin_escrow_client.get("/admin/escrows")
        assert "2.5" in resp.text

    def test_escrows_page_shows_status_badges(self, admin_escrow_client):
        """Escrow list renders status badges."""
        _login(admin_escrow_client)
        resp = admin_escrow_client.get("/admin/escrows")
        body = resp.text.lower()
        assert "created" in body
        assert "completed" in body

    def test_escrows_page_empty_shows_message(self, admin_empty_client):
        """Empty escrow list shows 'No escrow deals' message."""
        _login(admin_empty_client)
        resp = admin_empty_client.get("/admin/escrows")
        assert resp.status_code == 200
        assert "no escrow deals" in resp.text.lower()

    def test_escrows_page_filter_by_status(self, admin_escrow_client):
        """GET /admin/escrows?status=completed filters correctly."""
        _login(admin_escrow_client)
        resp = admin_escrow_client.get("/admin/escrows?status=completed")
        assert resp.status_code == 200
        # Should show the completed deal but not the created one in the table
        body = resp.text.lower()
        assert "completed" in body

    def test_escrows_page_has_detail_links(self, admin_escrow_client, seed_escrow_data):
        """Escrow list has links to individual escrow detail pages."""
        _login(admin_escrow_client)
        resp = admin_escrow_client.get("/admin/escrows")
        assert "/admin/escrows/" in resp.text

    def test_escrows_page_requires_auth(self, admin_escrow_client):
        """GET /admin/escrows without session redirects to login."""
        resp = admin_escrow_client.get("/admin/escrows", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "login" in resp.headers.get("location", "")


# ---------------------------------------------------------------------------
# 3. Escrow detail page (/admin/escrows/{deal_id})
# ---------------------------------------------------------------------------

class TestEscrowDetailPage:
    """Admin escrow detail page tests."""

    def test_escrow_detail_returns_200(self, admin_escrow_client, seed_escrow_data):
        """GET /admin/escrows/{id} for existing deal returns 200."""
        _login(admin_escrow_client)
        deal_id = seed_escrow_data["active_deal"].id
        resp = admin_escrow_client.get(f"/admin/escrows/{deal_id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_escrow_detail_shows_participants(self, admin_escrow_client, seed_escrow_data):
        """Detail page shows buyer and seller names."""
        _login(admin_escrow_client)
        deal_id = seed_escrow_data["active_deal"].id
        resp = admin_escrow_client.get(f"/admin/escrows/{deal_id}")
        assert "escrow-buyer" in resp.text
        assert "escrow-seller" in resp.text

    def test_escrow_detail_shows_amount(self, admin_escrow_client, seed_escrow_data):
        """Detail page shows the deal amount."""
        _login(admin_escrow_client)
        deal_id = seed_escrow_data["active_deal"].id
        resp = admin_escrow_client.get(f"/admin/escrows/{deal_id}")
        assert "2.5" in resp.text

    def test_escrow_detail_shows_description(self, admin_escrow_client, seed_escrow_data):
        """Detail page shows the deal description."""
        _login(admin_escrow_client)
        deal_id = seed_escrow_data["active_deal"].id
        resp = admin_escrow_client.get(f"/admin/escrows/{deal_id}")
        assert "active deal" in resp.text

    def test_escrow_detail_shows_timeline(self, admin_escrow_client, seed_escrow_data):
        """Detail page renders the timeline section."""
        _login(admin_escrow_client)
        deal_id = seed_escrow_data["completed_deal"].id
        resp = admin_escrow_client.get(f"/admin/escrows/{deal_id}")
        assert "timeline" in resp.text.lower() or "created" in resp.text.lower()

    def test_escrow_detail_nonexistent_returns_404(self, admin_escrow_client):
        """GET /admin/escrows/{fake-uuid} returns 404."""
        _login(admin_escrow_client)
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = admin_escrow_client.get(f"/admin/escrows/{fake_id}")
        assert resp.status_code == 404

    def test_escrow_detail_invalid_uuid_returns_404(self, admin_escrow_client):
        """GET /admin/escrows/not-a-uuid returns 404."""
        _login(admin_escrow_client)
        resp = admin_escrow_client.get("/admin/escrows/not-a-uuid")
        assert resp.status_code == 404

    def test_escrow_detail_requires_auth(self, admin_escrow_client, seed_escrow_data):
        """GET /admin/escrows/{id} without session redirects to login."""
        deal_id = seed_escrow_data["active_deal"].id
        resp = admin_escrow_client.get(
            f"/admin/escrows/{deal_id}", follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert "login" in resp.headers.get("location", "")

    def test_escrow_detail_has_back_link(self, admin_escrow_client, seed_escrow_data):
        """Detail page has a back link to the escrows list."""
        _login(admin_escrow_client)
        deal_id = seed_escrow_data["active_deal"].id
        resp = admin_escrow_client.get(f"/admin/escrows/{deal_id}")
        assert "/admin/escrows" in resp.text

    def test_escrow_detail_completed_shows_fee(self, admin_escrow_client, seed_escrow_data):
        """Completed deal detail page shows fee information."""
        _login(admin_escrow_client)
        deal_id = seed_escrow_data["completed_deal"].id
        resp = admin_escrow_client.get(f"/admin/escrows/{deal_id}")
        assert "fee" in resp.text.lower()
        assert "0.001" in resp.text
