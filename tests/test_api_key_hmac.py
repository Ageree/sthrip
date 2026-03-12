"""Tests for HMAC-based API key hashing."""
import hmac
import hashlib
import os
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    HubRoute, FeeCollection, PendingWithdrawal,
)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Agent.__table__, AgentReputation.__table__])
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_hmac_hash_differs_from_sha256():
    """HMAC hash must differ from plain SHA-256."""
    from sthrip.db.repository import AgentRepository
    api_key = "sk_test_key_1234"
    sha256_hash = hashlib.sha256(api_key.encode()).hexdigest()
    hmac_hash = AgentRepository._hash_api_key(api_key)
    assert hmac_hash != sha256_hash


def test_hmac_hash_deterministic():
    """Same key must produce same hash."""
    from sthrip.db.repository import AgentRepository
    key = "sk_test_deterministic"
    assert AgentRepository._hash_api_key(key) == AgentRepository._hash_api_key(key)


def test_hmac_hash_uses_server_secret():
    """Changing server secret must change hash."""
    from sthrip.db.repository import AgentRepository
    key = "sk_test_secret_change"
    hash1 = AgentRepository._hash_api_key(key)
    with patch("sthrip.db.agent_repo._get_hmac_secret", return_value="different_secret"):
        hash2 = AgentRepository._hash_api_key(key)
    assert hash1 != hash2


def test_create_agent_uses_hmac(db_session):
    """AgentRepository.create_agent must store HMAC hash, not SHA-256."""
    from sthrip.db.repository import AgentRepository
    repo = AgentRepository(db_session)
    agent, creds = repo.create_agent("hmac_test_agent")
    plain_key = creds["api_key"]
    sha256 = hashlib.sha256(plain_key.encode()).hexdigest()
    assert agent.api_key_hash != sha256
    assert agent.api_key_hash == AgentRepository._hash_api_key(plain_key)


def test_get_by_api_key_uses_hmac(db_session):
    """Lookup must use HMAC hash."""
    from sthrip.db.repository import AgentRepository
    repo = AgentRepository(db_session)
    agent, creds = repo.create_agent("hmac_lookup_agent")
    db_session.flush()
    found = repo.get_by_api_key(creds["api_key"])
    assert found is not None
    assert found.id == agent.id


# ═══════════════════════════════════════════════════════════════════════════════
# Integration fixtures for rotate-key endpoint test
# ═══════════════════════════════════════════════════════════════════════════════

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    PendingWithdrawal.__table__,
]


@pytest.fixture
def api_client():
    """FastAPI test client with in-memory SQLite and all external deps mocked."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def get_test_db():
        session = Session()
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
    mock_limiter.check_ip_rate_limit.return_value = None
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-03-03T00:00:00",
        "checks": {},
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    from cryptography.fernet import Fernet
    test_fernet_key = Fernet.generate_key().decode()

    from contextlib import ExitStack
    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                os.environ,
                {"HUB_MODE": "ledger", "WEBHOOK_ENCRYPTION_KEY": test_fernet_key},
            )
        )
        # Reset Fernet singleton so the new key is picked up
        stack.enter_context(patch("sthrip.crypto._fernet_instance", None))
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
        for mod in [
            "sthrip.services.rate_limiter",
            "api.deps",
            "api.routers.agents",
            "api.main_v2",
        ]:
            stack.enter_context(
                patch(f"{mod}.get_rate_limiter", return_value=mock_limiter)
            )
        stack.enter_context(
            patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor)
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
        for mod in [
            "api.deps",
            "api.routers.agents",
            "api.routers.payments",
            "api.routers.balance",
            "api.routers.admin",
            "api.main_v2",
        ]:
            stack.enter_context(patch(f"{mod}.audit_log"))

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def agent_headers(api_client):
    """Register a fresh agent and return its auth headers."""
    resp = api_client.post(
        "/v2/agents/register",
        json={"agent_name": "rotate-key-agent", "xmr_address": "5" + "a" * 94},
    )
    assert resp.status_code == 201, f"Registration failed: {resp.text}"
    api_key = resp.json()["api_key"]
    return {"Authorization": f"Bearer {api_key}"}


# ═══════════════════════════════════════════════════════════════════════════════
# C1: rotate-key must use HMAC-SHA256, not plain SHA-256
# ═══════════════════════════════════════════════════════════════════════════════

def test_rotate_api_key_uses_hmac_hash(api_client, agent_headers):
    """C1: rotate-key must use HMAC-SHA256, not plain SHA-256."""
    resp = api_client.post("/v2/me/rotate-key", headers=agent_headers)
    assert resp.status_code == 200
    new_key = resp.json()["api_key"]

    new_headers = {"Authorization": f"Bearer {new_key}"}
    profile_resp = api_client.get("/v2/me", headers=new_headers)
    assert profile_resp.status_code == 200, (
        "Agent locked out after key rotation — hash mismatch (C1 bug)"
    )


def test_rotate_api_key_invalidates_old_key(api_client, agent_headers):
    """After rotation, old key must be rejected (single-transaction guarantee)."""
    resp = api_client.post("/v2/me/rotate-key", headers=agent_headers)
    assert resp.status_code == 200

    # Old key should no longer work
    old_key_resp = api_client.get("/v2/me", headers=agent_headers)
    assert old_key_resp.status_code == 401, (
        "Old API key still works after rotation — possible multi-session bug"
    )


def test_rotate_api_key_uses_di_session(api_client, agent_headers):
    """Rotate-key must use DI-injected session, not open a second get_db() session."""
    import inspect
    from api.routers.agents import rotate_api_key

    sig = inspect.signature(rotate_api_key)
    params = sig.parameters
    # Must have a 'db' parameter injected via Depends
    assert "db" in params, (
        "rotate_api_key must accept 'db: Session = Depends(get_db_session)' for single-session guarantee"
    )
