"""
E2E tests for production readiness features.

Covers: readiness endpoint, idempotency (atomic reserve/store),
error sanitization, Monero address validation, and migration fail-fast.
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
    Base, Agent, AgentReputation, AgentBalance, HubRoute, FeeCollection,
)

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
]

_VALID_STAGENET_ADDR = "5" + "a" * 94
_VALID_INTEGRATED_ADDR = "5" + "b" * 105


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
    mock_limiter.check_ip_rate_limit.return_value = None
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-03-05T00:00:00",
        "checks": {},
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger", "MONERO_NETWORK": "stagenet"}))

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


def _register_and_fund(client, name, amount=10.0):
    """Helper: register agent, deposit funds, return (api_key, agent_id)."""
    r = client.post("/v2/agents/register", json={
        "agent_name": name,
        "xmr_address": _VALID_STAGENET_ADDR,
    })
    assert r.status_code == 201
    key = r.json()["api_key"]
    agent_id = r.json()["agent_id"]
    h = {"Authorization": f"Bearer {key}"}

    client.post("/v2/balance/deposit", json={"amount": amount}, headers=h)
    return key, agent_id


# ═══════════════════════════════════════════════════════════════════════════════
# READINESS ENDPOINT
# ═══════════════════════════════════════════════════════════════════════════════

class TestReadinessEndpoint:
    """GET /ready — DB connectivity check."""

    def test_ready_returns_200_when_db_is_up(self, client):
        r = client.get("/ready")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["checks"]["database"] == "ok"

    def test_ready_does_not_require_auth(self, client):
        """Readiness endpoint is public (no Authorization header)."""
        r = client.get("/ready")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# IDEMPOTENCY — ATOMIC RESERVE/STORE
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdempotencyE2E:
    """Idempotency key behavior across endpoints."""

    def test_deposit_idempotency_returns_same_response(self, client):
        """Same idempotency key returns cached response on second call."""
        key, _ = _register_and_fund(client, "idem-deposit-agent", amount=0)
        h = {"Authorization": f"Bearer {key}", "Idempotency-Key": "dep-001"}

        r1 = client.post("/v2/balance/deposit", json={"amount": 5.0}, headers=h)
        assert r1.status_code == 200

        r2 = client.post("/v2/balance/deposit", json={"amount": 5.0}, headers=h)
        assert r2.status_code == 200
        assert r1.json() == r2.json()

        # Balance should only be credited once
        r = client.get("/v2/balance", headers={"Authorization": f"Bearer {key}"})
        assert r.json()["available"] == 5.0

    def test_withdraw_idempotency_returns_same_response(self, client):
        key, _ = _register_and_fund(client, "idem-withdraw-agent", amount=20.0)
        h = {"Authorization": f"Bearer {key}", "Idempotency-Key": "wd-001"}

        r1 = client.post("/v2/balance/withdraw",
                         json={"amount": 3.0, "address": _VALID_STAGENET_ADDR}, headers=h)
        assert r1.status_code == 200

        r2 = client.post("/v2/balance/withdraw",
                         json={"amount": 3.0, "address": _VALID_STAGENET_ADDR}, headers=h)
        assert r2.status_code == 200
        assert r1.json() == r2.json()

        # Balance should only be deducted once
        r = client.get("/v2/balance", headers={"Authorization": f"Bearer {key}"})
        assert r.json()["available"] == 17.0

    def test_hub_payment_idempotency(self, client):
        sender_key, _ = _register_and_fund(client, "idem-sender", amount=50.0)
        client.post("/v2/agents/register", json={
            "agent_name": "idem-receiver",
            "xmr_address": _VALID_STAGENET_ADDR,
        })

        h = {"Authorization": f"Bearer {sender_key}", "Idempotency-Key": "hub-001"}

        r1 = client.post("/v2/payments/hub-routing",
                         json={"to_agent_name": "idem-receiver", "amount": 10.0}, headers=h)
        assert r1.status_code == 200

        r2 = client.post("/v2/payments/hub-routing",
                         json={"to_agent_name": "idem-receiver", "amount": 10.0}, headers=h)
        assert r2.status_code == 200
        assert r1.json()["payment_id"] == r2.json()["payment_id"]

    def test_different_idempotency_keys_process_independently(self, client):
        key, _ = _register_and_fund(client, "idem-multi-agent", amount=20.0)
        h1 = {"Authorization": f"Bearer {key}", "Idempotency-Key": "dep-A"}
        h2 = {"Authorization": f"Bearer {key}", "Idempotency-Key": "dep-B"}

        r1 = client.post("/v2/balance/deposit", json={"amount": 5.0}, headers=h1)
        r2 = client.post("/v2/balance/deposit", json={"amount": 5.0}, headers=h2)
        assert r1.status_code == 200
        assert r2.status_code == 200

        r = client.get("/v2/balance", headers={"Authorization": f"Bearer {key}"})
        assert r.json()["available"] == 30.0  # 20 + 5 + 5


# ═══════════════════════════════════════════════════════════════════════════════
# ERROR SANITIZATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestErrorSanitization:
    """Verify internal error details are NOT leaked to clients."""

    def test_registration_error_is_generic(self, client):
        """Duplicate registration returns generic message, not ValueError details."""
        client.post("/v2/agents/register", json={"agent_name": "dupe-agent"})
        r = client.post("/v2/agents/register", json={"agent_name": "dupe-agent"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        # Should NOT contain internal error details like class names or tracebacks
        assert "Registration failed" in detail
        assert "ValueError" not in detail
        assert "Traceback" not in detail

    def test_withdraw_insufficient_does_not_leak_internals(self, client):
        key, _ = _register_and_fund(client, "leak-test-agent", amount=1.0)
        h = {"Authorization": f"Bearer {key}"}
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 999.0, "address": _VALID_STAGENET_ADDR}, headers=h)
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "Insufficient" in detail
        # Should not contain SQL or internal state details
        assert "sqlalchemy" not in detail.lower()
        assert "SELECT" not in detail


# ═══════════════════════════════════════════════════════════════════════════════
# MONERO ADDRESS VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestMoneroAddressValidationE2E:
    """Withdraw endpoint rejects invalid Monero addresses."""

    def test_valid_stagenet_standard_address(self, client):
        key, _ = _register_and_fund(client, "addr-valid-std", amount=10.0)
        h = {"Authorization": f"Bearer {key}"}
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 1.0, "address": _VALID_STAGENET_ADDR}, headers=h)
        assert r.status_code == 200

    def test_valid_integrated_address(self, client):
        key, _ = _register_and_fund(client, "addr-valid-int", amount=10.0)
        h = {"Authorization": f"Bearer {key}"}
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 1.0, "address": _VALID_INTEGRATED_ADDR}, headers=h)
        assert r.status_code == 200

    def test_rejects_mainnet_address_on_stagenet(self, client):
        """Mainnet prefix (4) should be rejected when MONERO_NETWORK=stagenet."""
        key, _ = _register_and_fund(client, "addr-mainnet", amount=10.0)
        h = {"Authorization": f"Bearer {key}"}
        mainnet_addr = "4" + "a" * 94
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 1.0, "address": mainnet_addr}, headers=h)
        assert r.status_code == 422

    def test_rejects_wrong_length(self, client):
        key, _ = _register_and_fund(client, "addr-bad-len", amount=10.0)
        h = {"Authorization": f"Bearer {key}"}
        bad_addr = "5" + "a" * 80  # 81 chars
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 1.0, "address": bad_addr}, headers=h)
        assert r.status_code == 422

    def test_rejects_invalid_base58_chars(self, client):
        key, _ = _register_and_fund(client, "addr-bad-b58", amount=10.0)
        h = {"Authorization": f"Bearer {key}"}
        bad_addr = "5" + "0" * 94  # '0' not in base58
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 1.0, "address": bad_addr}, headers=h)
        assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# FULL E2E: REGISTER → DEPOSIT → PAY → WITHDRAW
# ═══════════════════════════════════════════════════════════════════════════════

class TestFullPaymentLifecycle:
    """Complete lifecycle with production-readiness features active."""

    def test_register_deposit_pay_withdraw(self, client):
        """
        1. Register Alice & Bob
        2. Alice deposits 100 XMR
        3. Alice pays Bob 30 XMR via hub
        4. Alice withdraws 20 XMR to external address
        5. Verify all balances are consistent
        """
        # Register
        alice_key, _ = _register_and_fund(client, "lifecycle-alice", amount=100.0)
        r = client.post("/v2/agents/register", json={
            "agent_name": "lifecycle-bob",
            "xmr_address": _VALID_STAGENET_ADDR,
        })
        bob_key = r.json()["api_key"]

        alice_h = {"Authorization": f"Bearer {alice_key}"}
        bob_h = {"Authorization": f"Bearer {bob_key}"}

        # Hub payment with idempotency
        pay_h = {**alice_h, "Idempotency-Key": "lifecycle-pay-001"}
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "lifecycle-bob", "amount": 30.0},
                        headers=pay_h)
        assert r.status_code == 200
        fee = r.json()["fee"]

        # Idempotent replay returns same result
        r2 = client.post("/v2/payments/hub-routing",
                         json={"to_agent_name": "lifecycle-bob", "amount": 30.0},
                         headers=pay_h)
        assert r2.json()["payment_id"] == r.json()["payment_id"]

        # Bob got 30
        r = client.get("/v2/balance", headers=bob_h)
        assert r.json()["available"] == 30.0

        # Alice withdraws 20
        wd_h = {**alice_h, "Idempotency-Key": "lifecycle-wd-001"}
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 20.0, "address": _VALID_STAGENET_ADDR},
                        headers=wd_h)
        assert r.status_code == 200

        # Verify Alice: 100 - 30 - fee - 20
        r = client.get("/v2/balance", headers=alice_h)
        alice_bal = r.json()["available"]
        expected = 100.0 - 30.0 - fee - 20.0
        assert abs(alice_bal - expected) < 0.001, f"Alice: {alice_bal} != {expected}"

        # Readiness still healthy
        r = client.get("/ready")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"
