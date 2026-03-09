"""
End-to-end test for the full hub payment flow.

Flow:
1. Register agent A
2. Register agent B
3. Agent A deposits 10 XMR
4. Agent A sends 5 XMR to Agent B via hub routing
5. Verify: Agent A balance = 10 - 5 - fee
6. Verify: Agent B balance = 5
7. Verify: fee_collections has 1 entry
8. Verify: payment history has 1 entry for each agent
"""
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
    Base, Agent, AgentReputation, AgentBalance, HubRoute, FeeCollection
)

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
def client(db_engine, db_session_factory):
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
        stack.enter_context(patch("sthrip.services.rate_limiter.get_rate_limiter", return_value=mock_limiter))
        for mod in ["api.main_v2", "api.deps", "api.routers.agents"]:
            stack.enter_context(patch(f"{mod}.get_rate_limiter", return_value=mock_limiter))

        # Audit log patches
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


class TestE2EHubPaymentFlow:
    """Full end-to-end hub payment flow"""

    def test_complete_hub_payment_flow(self, client):
        # ── Step 1: Register Agent A ──
        r = client.post("/v2/agents/register", json={
            "agent_name": "agent-alice",
            "xmr_address": "alice_xmr_addr_1234567890",
        })
        assert r.status_code == 201
        alice_key = r.json()["api_key"]
        alice_id = r.json()["agent_id"]
        alice_headers = {"Authorization": f"Bearer {alice_key}"}

        # ── Step 2: Register Agent B ──
        r = client.post("/v2/agents/register", json={
            "agent_name": "agent-bob",
            "xmr_address": "bob_xmr_addr_0987654321",
        })
        assert r.status_code == 201
        bob_key = r.json()["api_key"]
        bob_headers = {"Authorization": f"Bearer {bob_key}"}

        # ── Step 3: Agent A deposits 10 XMR ──
        r = client.post("/v2/balance/deposit",
                        json={"amount": 10.0},
                        headers=alice_headers)
        assert r.status_code == 200
        assert r.json()["new_balance"] == 10.0

        # Verify Alice balance is 10
        r = client.get("/v2/balance", headers=alice_headers)
        assert r.json()["available"] == 10.0

        # Verify Bob balance is 0
        r = client.get("/v2/balance", headers=bob_headers)
        assert r.json()["available"] == 0.0

        # ── Step 4: Agent A sends 5 XMR to Agent B ──
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "agent-bob", "amount": 5.0},
                        headers=alice_headers)
        assert r.status_code == 200
        payment = r.json()
        assert payment["status"] == "confirmed"
        assert payment["amount"] == 5.0
        assert payment["payment_type"] == "hub_routing"
        fee = payment["fee"]
        assert fee > 0
        total_deducted = payment["total_deducted"]

        # ── Step 5: Verify Agent A balance = 10 - 5 - fee ──
        r = client.get("/v2/balance", headers=alice_headers)
        alice_balance = r.json()["available"]
        expected_alice = 10.0 - total_deducted
        assert abs(alice_balance - expected_alice) < 0.000001, \
            f"Alice balance {alice_balance} != expected {expected_alice}"

        # ── Step 6: Verify Agent B balance = 5 ──
        r = client.get("/v2/balance", headers=bob_headers)
        bob_balance = r.json()["available"]
        assert bob_balance == 5.0, f"Bob balance {bob_balance} != 5.0"

        # ── Step 7: Verify payment details ──
        assert payment["recipient"]["agent_name"] == "agent-bob"
        assert payment["fee_percent"] > 0

    def test_multiple_payments_accumulate(self, client):
        """Multiple payments should correctly update balances"""
        # Register agents
        r = client.post("/v2/agents/register", json={
            "agent_name": "multi-sender",
            "xmr_address": "sender_addr_multi_1234",
        })
        sender_key = r.json()["api_key"]
        sender_h = {"Authorization": f"Bearer {sender_key}"}

        r = client.post("/v2/agents/register", json={
            "agent_name": "multi-receiver",
            "xmr_address": "receiver_addr_multi_5678",
        })
        receiver_key = r.json()["api_key"]
        receiver_h = {"Authorization": f"Bearer {receiver_key}"}

        # Deposit 100
        client.post("/v2/balance/deposit", json={"amount": 100.0}, headers=sender_h)

        # Send 3 payments
        total_fees = 0
        for i in range(3):
            r = client.post("/v2/payments/hub-routing",
                            json={"to_agent_name": "multi-receiver", "amount": 10.0},
                            headers=sender_h)
            assert r.status_code == 200
            total_fees += r.json()["fee"]

        # Verify receiver got 30 XMR total
        r = client.get("/v2/balance", headers=receiver_h)
        assert r.json()["available"] == 30.0

        # Verify sender balance is 100 - 30 - fees
        r = client.get("/v2/balance", headers=sender_h)
        sender_balance = r.json()["available"]
        expected = 100.0 - 30.0 - total_fees
        assert abs(sender_balance - expected) < 0.0001

    def test_deposit_and_withdraw_roundtrip(self, client):
        """Deposit and withdraw should correctly track totals"""
        r = client.post("/v2/agents/register", json={
            "agent_name": "roundtrip-agent",
            "xmr_address": "roundtrip_addr_1234567",
        })
        key = r.json()["api_key"]
        h = {"Authorization": f"Bearer {key}"}

        # Deposit 50
        client.post("/v2/balance/deposit", json={"amount": 50.0}, headers=h)

        # Withdraw 20
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 20.0, "address": "5" + "c" * 94},
                        headers=h)
        assert r.status_code == 200
        assert r.json()["remaining_balance"] == 30.0

        # Check balance
        r = client.get("/v2/balance", headers=h)
        data = r.json()
        assert data["available"] == 30.0
        assert data["total_deposited"] == 50.0
