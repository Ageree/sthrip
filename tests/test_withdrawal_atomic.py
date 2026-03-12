"""Tests for atomic withdrawal with pending state."""

import os
import pytest
from decimal import Decimal
from uuid import uuid4
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, PendingWithdrawal,
    HubRoute, FeeCollection, Transaction,
    AgentTier, RateLimitTier, PrivacyLevel,
)
from sthrip.db.repository import PendingWithdrawalRepository, BalanceRepository

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    PendingWithdrawal.__table__,
]

_INTEGRATION_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    Transaction.__table__,
    PendingWithdrawal.__table__,
]


@pytest.fixture
def db_engine_integration():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_INTEGRATION_TEST_TABLES)
    return engine


@pytest.fixture
def db_session_factory_integration(db_engine_integration):
    return sessionmaker(bind=db_engine_integration, expire_on_commit=False)


@pytest.fixture
def mock_wallet_service_integration():
    svc = MagicMock()
    svc.send_withdrawal.return_value = {"tx_hash": "abc123", "fee": Decimal("0.001")}
    return svc


@pytest.fixture
def onchain_client_integration(db_engine_integration, db_session_factory_integration, mock_wallet_service_integration):
    """FastAPI test client in onchain mode for atomicity integration tests."""

    @contextmanager
    def get_test_db():
        session = db_session_factory_integration()
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
        "status": "healthy", "timestamp": "2026-03-03T00:00:00", "checks": {}
    }
    mock_monitor.get_alerts.return_value = []
    mock_webhook_svc = MagicMock()
    mock_webhook_svc.get_delivery_stats.return_value = {"total": 0}

    from contextlib import ExitStack
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "HUB_MODE": "onchain",
            "MONERO_NETWORK": "stagenet",
            "MONERO_MIN_CONFIRMATIONS": "10",
        }))
        for mod in [
            "sthrip.db.database",
            "sthrip.services.agent_registry",
            "sthrip.services.fee_collector",
            "sthrip.services.webhook_service",
            "api.deps",
            "api.routers.health",
            "api.routers.agents",
            "api.routers.payments",
            "api.routers.balance",
            "api.routers.webhooks",
        ]:
            stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))
        stack.enter_context(patch("sthrip.db.database.create_tables"))
        for mod in ["sthrip.services.rate_limiter", "api.deps", "api.routers.agents", "api.main_v2"]:
            stack.enter_context(patch(f"{mod}.get_rate_limiter", return_value=mock_limiter))
        stack.enter_context(patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor))
        stack.enter_context(patch("sthrip.services.monitoring.setup_default_monitoring", return_value=mock_monitor))
        stack.enter_context(patch("sthrip.services.webhook_service.get_webhook_service", return_value=mock_webhook_svc))
        stack.enter_context(patch("sthrip.services.webhook_service.queue_webhook"))
        stack.enter_context(patch("api.helpers.get_wallet_service", return_value=mock_wallet_service_integration))
        stack.enter_context(patch("api.routers.balance.get_wallet_service", return_value=mock_wallet_service_integration))
        for mod in ["api.deps", "api.routers.agents", "api.routers.payments", "api.routers.balance", "api.routers.admin", "api.main_v2"]:
            stack.enter_context(patch(f"{mod}.audit_log"))

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def agent_headers(onchain_client_integration):
    r = onchain_client_integration.post("/v2/agents/register", json={
        "agent_name": "atomic-test-agent",
        "xmr_address": "5" + "A" * 94,
    })
    assert r.status_code == 201, f"Registration failed: {r.text}"
    api_key = r.json()["api_key"]
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture
def client(onchain_client_integration):
    return onchain_client_integration


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _create_agent(session, agent_name="test-agent"):
    agent = Agent(
        id=uuid4(), agent_name=agent_name, is_active=True,
        tier=AgentTier.FREE, rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.MEDIUM,
    )
    session.add(agent)
    session.flush()
    return agent


def test_withdrawal_creates_pending_record(db_session):
    """Withdrawal must create a pending record before calling RPC."""
    agent = _create_agent(db_session)
    repo = PendingWithdrawalRepository(db_session)
    pw = repo.create(
        agent_id=agent.id,
        amount=Decimal("1.5"),
        address="addr123",
    )
    assert pw.status == "pending"
    assert pw.amount == Decimal("1.5")


def test_withdrawal_marks_completed(db_session):
    """After RPC success, pending withdrawal must be marked completed."""
    agent = _create_agent(db_session)
    repo = PendingWithdrawalRepository(db_session)
    pw = repo.create(agent_id=agent.id, amount=Decimal("1.5"), address="addr123")
    repo.mark_completed(pw.id, tx_hash="abc123")
    updated = repo.get_by_id(pw.id)
    assert updated.status == "completed"
    assert updated.tx_hash == "abc123"


def test_withdrawal_marks_failed(db_session):
    """On RPC failure, mark failed."""
    agent = _create_agent(db_session)
    repo = PendingWithdrawalRepository(db_session)
    pw = repo.create(agent_id=agent.id, amount=Decimal("1.5"), address="addr123")
    repo.mark_failed(pw.id, error="RPC timeout")
    updated = repo.get_by_id(pw.id)
    assert updated.status == "failed"
    assert updated.error == "RPC timeout"


def test_get_pending_returns_only_pending(db_session):
    """get_pending should only return pending records."""
    agent = _create_agent(db_session)
    repo = PendingWithdrawalRepository(db_session)

    pw1 = repo.create(agent_id=agent.id, amount=Decimal("1.0"), address="addr1")
    pw2 = repo.create(agent_id=agent.id, amount=Decimal("2.0"), address="addr2")
    repo.mark_completed(pw1.id, tx_hash="tx1")

    pending = repo.get_pending()
    assert len(pending) == 1
    assert pending[0].id == pw2.id


# ─── I2: Atomicity integration test ──────────────────────────────────────────

def test_withdrawal_completion_is_atomic(
    client, agent_headers, mock_wallet_service_integration,
    db_engine_integration, db_session_factory_integration
):
    """I2: mark_completed + create_transaction + fresh balance read must be in one session.

    We count how many times get_db() is entered AFTER the RPC call returns.
    Before the fix there are two consecutive sessions (mark_completed, then
    fresh-balance-read). After the fix there must be exactly one.
    """
    # Fund the agent in ledger mode by directly writing to the DB.
    with db_session_factory_integration() as db:
        agent = db.query(Agent).filter(Agent.agent_name == "atomic-test-agent").first()
        if agent:
            repo = BalanceRepository(db)
            repo.deposit(agent.id, Decimal("10.0"))
            db.commit()

    # Count get_db sessions that open during the completion phase.
    # We wrap the real get_db (which is already patched to get_test_db in the
    # onchain_client_integration fixture) by monkey-patching the router module.
    import api.routers.balance as balance_router

    original_get_db = balance_router.get_db
    call_log = []

    @contextmanager
    def counting_get_db():
        call_log.append("open")
        with original_get_db() as s:
            yield s

    # Intercept only during the withdraw call; restore afterwards.
    balance_router.get_db = counting_get_db
    try:
        resp = client.post(
            "/v2/balance/withdraw",
            json={"amount": 1.0, "address": "5" + "A" * 94},
            headers=agent_headers,
        )
    finally:
        balance_router.get_db = original_get_db

    assert resp.status_code == 200, f"Unexpected status: {resp.text}"
    assert resp.json()["tx_hash"] == "abc123"

    # The withdraw endpoint opens sessions for:
    #   1. deduct + create pending  (always 1)
    #   2. mark_completed + create transaction + fresh balance  (should be 1, was 2)
    # Total expected after fix: 2 sessions.
    # Before fix: 3 sessions (mark_completed, create_transaction merged but fresh balance separate).
    # NOTE: The initial deduct+pending session runs before RPC so it is counted too.
    # We assert <= 2 total sessions, proving completion + balance read are merged.
    assert len(call_log) <= 2, (
        f"Expected at most 2 DB sessions for withdraw, got {len(call_log)}. "
        "mark_completed, create_transaction, and fresh balance read must share one session."
    )
