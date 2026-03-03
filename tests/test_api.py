"""Integration tests for the StealthPay API"""
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock, PropertyMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool
from contextlib import contextmanager

from stealthpay.db.models import (
    Base, Agent, AgentReputation, AgentBalance, AgentTier,
    RateLimitTier, PrivacyLevel, HubRoute, FeeCollection
)

# Only create tables that work with SQLite
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
        poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    return engine


@pytest.fixture
def db_session_factory(db_engine):
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def client(db_engine, db_session_factory):
    """FastAPI test client with mocked dependencies"""

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

    # Mock rate limiter to always allow
    mock_limiter = MagicMock()
    mock_limiter.check_rate_limit.return_value = None
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    # Mock monitor
    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-03-03T00:00:00",
        "checks": {}
    }
    mock_monitor.get_alerts.return_value = []

    # Mock webhook
    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with patch("stealthpay.db.database.get_db", side_effect=get_test_db), \
         patch("stealthpay.services.agent_registry.get_db", side_effect=get_test_db), \
         patch("stealthpay.services.fee_collector.get_db", side_effect=get_test_db), \
         patch("stealthpay.services.webhook_service.get_db", side_effect=get_test_db), \
         patch("api.main_v2.get_db", side_effect=get_test_db), \
         patch("stealthpay.db.database.create_tables"), \
         patch("stealthpay.services.rate_limiter.get_rate_limiter", return_value=mock_limiter), \
         patch("stealthpay.services.monitoring.get_monitor", return_value=mock_monitor), \
         patch("stealthpay.services.monitoring.setup_default_monitoring", return_value=mock_monitor), \
         patch("stealthpay.services.webhook_service.get_webhook_service", return_value=mock_webhook), \
         patch("stealthpay.services.webhook_service.queue_webhook"):

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def registered_agent(client):
    """Register an agent and return (api_key, agent_name)"""
    r = client.post("/v2/agents/register", json={
        "agent_name": "test-sender",
        "xmr_address": "test_xmr_address_12345"
    })
    assert r.status_code == 201, f"Registration failed: {r.text}"
    return r.json()["api_key"], "test-sender"


@pytest.fixture
def two_agents(client):
    """Register sender and recipient, return (sender_key, recipient_key)"""
    r1 = client.post("/v2/agents/register", json={
        "agent_name": "sender-agent",
        "xmr_address": "sender_xmr_addr_1234"
    })
    assert r1.status_code == 201
    sender_key = r1.json()["api_key"]

    r2 = client.post("/v2/agents/register", json={
        "agent_name": "receiver-agent",
        "xmr_address": "receiver_xmr_addr_5678"
    })
    assert r2.status_code == 201
    receiver_key = r2.json()["api_key"]

    return sender_key, receiver_key


class TestPublicEndpoints:
    def test_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "StealthPay" in r.json()["name"]

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"


class TestRegistration:
    def test_register_agent(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "new-agent",
            "xmr_address": "some_xmr_address"
        })
        assert r.status_code == 201
        data = r.json()
        assert "api_key" in data
        assert data["api_key"].startswith("sk_")
        assert data["agent_name"] == "new-agent"

    def test_register_invalid_name(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "bad name!",
            "xmr_address": "addr"
        })
        assert r.status_code == 422  # Pydantic validation

    def test_register_short_name(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "ab",
            "xmr_address": "addr"
        })
        assert r.status_code == 422


class TestAuthentication:
    def test_unauthenticated_request(self, client):
        r = client.get("/v2/me")
        assert r.status_code == 401

    def test_invalid_api_key(self, client):
        r = client.get("/v2/me", headers={"Authorization": "Bearer invalid_key"})
        assert r.status_code == 401

    def test_valid_api_key(self, client, registered_agent):
        key, name = registered_agent
        r = client.get("/v2/me", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        assert r.json()["agent_name"] == name


class TestBalance:
    def test_initial_balance_zero(self, client, registered_agent):
        key, _ = registered_agent
        r = client.get("/v2/balance", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        assert r.json()["available"] == 0

    def test_deposit(self, client, registered_agent):
        key, _ = registered_agent
        r = client.post("/v2/balance/deposit",
                        json={"amount": 10.0},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        assert r.json()["new_balance"] == 10.0

    def test_deposit_invalid_amount(self, client, registered_agent):
        key, _ = registered_agent
        r = client.post("/v2/balance/deposit",
                        json={"amount": -5.0},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 422

    def test_withdraw(self, client, registered_agent):
        key, _ = registered_agent
        # Deposit first
        client.post("/v2/balance/deposit",
                    json={"amount": 10.0},
                    headers={"Authorization": f"Bearer {key}"})
        # Withdraw
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 3.0, "address": "some_xmr_address_to_withdraw"},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        assert r.json()["remaining_balance"] == 7.0

    def test_withdraw_insufficient(self, client, registered_agent):
        key, _ = registered_agent
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 100.0, "address": "some_xmr_address_to_withdraw"},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 400
        assert "Insufficient" in r.json()["detail"]


class TestHubRouting:
    def test_hub_payment_success(self, client, two_agents):
        sender_key, receiver_key = two_agents
        # Deposit
        client.post("/v2/balance/deposit",
                    json={"amount": 10.0},
                    headers={"Authorization": f"Bearer {sender_key}"})
        # Pay
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "receiver-agent", "amount": 5.0},
                        headers={"Authorization": f"Bearer {sender_key}"})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "confirmed"
        assert data["amount"] == 5.0
        assert data["fee"] > 0

    def test_hub_payment_insufficient_balance(self, client, two_agents):
        sender_key, _ = two_agents
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "receiver-agent", "amount": 100.0},
                        headers={"Authorization": f"Bearer {sender_key}"})
        assert r.status_code == 400
        assert "Insufficient" in r.json()["detail"]

    def test_hub_payment_unknown_recipient(self, client, registered_agent):
        key, _ = registered_agent
        client.post("/v2/balance/deposit",
                    json={"amount": 10.0},
                    headers={"Authorization": f"Bearer {key}"})
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "nonexistent-agent", "amount": 1.0},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 404

    def test_hub_payment_balances_correct(self, client, two_agents):
        sender_key, receiver_key = two_agents
        # Deposit 10 to sender
        client.post("/v2/balance/deposit",
                    json={"amount": 10.0},
                    headers={"Authorization": f"Bearer {sender_key}"})
        # Send 5
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "receiver-agent", "amount": 5.0},
                        headers={"Authorization": f"Bearer {sender_key}"})
        fee = r.json()["fee"]

        # Check sender balance
        r = client.get("/v2/balance", headers={"Authorization": f"Bearer {sender_key}"})
        sender_balance = r.json()["available"]
        assert abs(sender_balance - (10.0 - 5.0 - fee)) < 0.0001

        # Check receiver balance
        r = client.get("/v2/balance", headers={"Authorization": f"Bearer {receiver_key}"})
        assert r.json()["available"] == 5.0


class TestDisabledEndpoints:
    def test_p2p_send_disabled(self, client):
        r = client.post("/v2/payments/send", json={})
        assert r.status_code == 501

    def test_escrow_disabled(self, client):
        r = client.post("/v2/escrow/create", json={})
        assert r.status_code == 501
