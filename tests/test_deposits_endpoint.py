"""Tests for GET /v2/balance/deposits, SystemState, and wallet health — TDD RED phase"""
import os
import contextlib
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from contextlib import contextmanager

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, Transaction,
    HubRoute, FeeCollection, TransactionStatus,
    AgentTier, RateLimitTier, PrivacyLevel,
)
from sthrip.db.repository import BalanceRepository, TransactionRepository


_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    Transaction.__table__,
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
def mock_wallet_service():
    svc = MagicMock()
    svc.get_or_create_deposit_address.return_value = "5FakeSubaddr"
    svc.get_wallet_info.return_value = {
        "balance": Decimal("100"),
        "unlocked_balance": Decimal("95"),
        "address": "5HubPrimaryAddr",
    }
    return svc


@pytest.fixture
def client(db_engine, db_session_factory, mock_wallet_service):
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
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy", "timestamp": "2026-03-03T00:00:00", "checks": {},
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "onchain", "MONERO_NETWORK": "stagenet"}))

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
        stack.enter_context(patch("sthrip.services.rate_limiter.get_rate_limiter", return_value=mock_limiter))
        for mod in ["api.main_v2", "api.deps", "api.routers.agents"]:
            stack.enter_context(patch(f"{mod}.get_rate_limiter", return_value=mock_limiter))

        # Wallet service patches
        for mod in ["api.main_v2", "api.helpers", "api.routers.balance"]:
            stack.enter_context(patch(f"{mod}.get_wallet_service", return_value=mock_wallet_service))

        # Audit log patches
        for mod in [
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


@pytest.fixture
def agent_with_deposits(client, db_session_factory):
    """Register agent and create some deposit transactions."""
    r = client.post("/v2/agents/register", json={
        "agent_name": "deposit-viewer",
        "xmr_address": "test_addr",
    })
    key = r.json()["api_key"]

    # Create deposit transactions directly in DB
    with db_session_factory() as session:
        agent = session.query(Agent).filter(Agent.agent_name == "deposit-viewer").first()
        tx_repo = TransactionRepository(session)

        tx_repo.create(
            tx_hash="tx_dep_001",
            network="stagenet",
            from_agent_id=None,
            to_agent_id=agent.id,
            amount=Decimal("5.0"),
            status="confirmed",
        )
        tx_repo.create(
            tx_hash="tx_dep_002",
            network="stagenet",
            from_agent_id=None,
            to_agent_id=agent.id,
            amount=Decimal("2.5"),
            status="pending",
        )
        session.commit()

    return key, "deposit-viewer"


# ═══════════════════════════════════════════════════════════════════════════════
# DEPOSITS LIST ENDPOINT
# ═══════════════════════════════════════════════════════════════════════════════

class TestDepositsEndpoint:
    def test_list_deposits(self, client, agent_with_deposits):
        key, _ = agent_with_deposits
        r = client.get(
            "/v2/balance/deposits",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert "deposits" in data
        assert len(data["deposits"]) == 2

    def test_deposit_fields(self, client, agent_with_deposits):
        key, _ = agent_with_deposits
        r = client.get(
            "/v2/balance/deposits",
            headers={"Authorization": f"Bearer {key}"},
        )
        deposit = r.json()["deposits"][0]
        assert "tx_hash" in deposit
        assert "amount" in deposit
        assert "status" in deposit
        assert "created_at" in deposit

    def test_deposits_limit(self, client, agent_with_deposits):
        key, _ = agent_with_deposits
        r = client.get(
            "/v2/balance/deposits?limit=1",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert len(r.json()["deposits"]) == 1

    def test_deposits_unauthenticated(self, client):
        r = client.get("/v2/balance/deposits")
        assert r.status_code == 401

    def test_deposits_empty(self, client):
        # Register fresh agent with no deposits
        r = client.post("/v2/agents/register", json={
            "agent_name": "no-deposits-agent",
            "xmr_address": "test_addr",
        })
        key = r.json()["api_key"]
        r = client.get(
            "/v2/balance/deposits",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200
        assert r.json()["deposits"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM STATE
# ═══════════════════════════════════════════════════════════════════════════════

class TestSystemState:
    def test_get_set_state(self):
        from sthrip.db.models import SystemState

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine, tables=[SystemState.__table__])
        Session = sessionmaker(bind=engine)
        db = Session()

        from sthrip.db.repository import SystemStateRepository
        repo = SystemStateRepository(db)

        # Initially empty
        assert repo.get("last_scanned_height") is None

        # Set value
        repo.set("last_scanned_height", "50000")
        db.flush()
        assert repo.get("last_scanned_height") == "50000"

        # Update value
        repo.set("last_scanned_height", "60000")
        db.flush()
        assert repo.get("last_scanned_height") == "60000"

        db.close()
