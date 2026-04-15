"""End-to-end tests for POST /v2/balance/withdraw via agent API key auth.

These tests exercise the full withdrawal flow through the FastAPI endpoint,
authenticating as a registered agent (not admin), covering:
- Successful ledger-mode withdrawal
- Insufficient balance rejection
- Missing auth rejection
- Minimum amount validation
- Idempotency key handling
- Balance consistency after withdrawal
"""

import os
import pytest
from decimal import Decimal
from contextlib import contextmanager, ExitStack
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, PendingWithdrawal,
    HubRoute, FeeCollection, Transaction,
)
from sthrip.db.repository import BalanceRepository

from tests.conftest import (
    _COMMON_TEST_TABLES, _GET_DB_MODULES, _RATE_LIMITER_MODULES,
    _AUDIT_LOG_MODULES, generate_test_monero_address,
)


@pytest.fixture
def withdraw_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_COMMON_TEST_TABLES)
    return engine


@pytest.fixture
def withdraw_session_factory(withdraw_engine):
    return sessionmaker(bind=withdraw_engine, expire_on_commit=False)


@pytest.fixture
def ledger_client(withdraw_engine, withdraw_session_factory):
    """FastAPI test client in ledger mode for withdrawal tests."""

    @contextmanager
    def get_test_db():
        session = withdraw_session_factory()
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
        "status": "healthy", "timestamp": "2026-03-03T00:00:00", "checks": {}
    }
    mock_monitor.get_alerts.return_value = []
    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger"}))

        for mod in _GET_DB_MODULES:
            stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))
        stack.enter_context(patch("sthrip.db.database.create_tables"))

        for mod in _RATE_LIMITER_MODULES:
            stack.enter_context(
                patch(f"{mod}.get_rate_limiter", return_value=mock_limiter)
            )
        # Also patch rate_limiter in balance router for deposit subaddress rate limit
        stack.enter_context(
            patch("api.routers.balance.get_rate_limiter", return_value=mock_limiter)
        )

        for mod in _AUDIT_LOG_MODULES:
            stack.enter_context(patch(f"{mod}.audit_log"))

        stack.enter_context(
            patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor)
        )
        stack.enter_context(
            patch("sthrip.services.monitoring.setup_default_monitoring", return_value=mock_monitor)
        )
        stack.enter_context(
            patch("sthrip.services.webhook_service.get_webhook_service", return_value=mock_webhook)
        )
        stack.enter_context(patch("sthrip.services.webhook_service.queue_webhook"))

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


def _register_agent(client, name="withdraw-test-agent"):
    """Register an agent and return (api_key, headers)."""
    addr = generate_test_monero_address(network_byte=24)
    r = client.post("/v2/agents/register", json={
        "agent_name": name,
        "xmr_address": addr,
    })
    assert r.status_code == 201, f"Registration failed: {r.text}"
    api_key = r.json()["api_key"]
    return api_key, {"Authorization": f"Bearer {api_key}"}


def _fund_agent(session_factory, agent_name, amount):
    """Directly fund an agent's balance in the DB."""
    with session_factory() as db:
        agent = db.query(Agent).filter(Agent.agent_name == agent_name).first()
        assert agent, f"Agent {agent_name} not found"
        repo = BalanceRepository(db)
        repo.deposit(agent.id, Decimal(str(amount)))
        db.commit()


class TestWithdrawAPI:
    """Tests for POST /v2/balance/withdraw with agent auth."""

    def test_withdraw_success_ledger(self, ledger_client, withdraw_session_factory):
        """Successful withdrawal in ledger mode returns correct response."""
        api_key, headers = _register_agent(ledger_client, "wd-success")
        _fund_agent(withdraw_session_factory, "wd-success", "5.0")

        dest = generate_test_monero_address(network_byte=24)
        resp = ledger_client.post(
            "/v2/balance/withdraw",
            json={"amount": 1.0, "address": dest},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "withdrawn"
        assert Decimal(data["amount"]) == Decimal("1")
        assert data["token"] == "XMR"
        assert "remaining_balance" in data

    def test_withdraw_deducts_balance(self, ledger_client, withdraw_session_factory):
        """Balance is correctly deducted after withdrawal."""
        api_key, headers = _register_agent(ledger_client, "wd-deduct")
        _fund_agent(withdraw_session_factory, "wd-deduct", "10.0")

        dest = generate_test_monero_address(network_byte=24)
        resp = ledger_client.post(
            "/v2/balance/withdraw",
            json={"amount": 3.0, "address": dest},
            headers=headers,
        )
        assert resp.status_code == 200

        # Check balance via API
        bal_resp = ledger_client.get("/v2/balance", headers=headers)
        assert bal_resp.status_code == 200
        available = Decimal(bal_resp.json()["available"])
        assert available == Decimal("7.0") or available == Decimal("7.000000000000")

    def test_withdraw_insufficient_balance(self, ledger_client, withdraw_session_factory):
        """Withdrawal with insufficient balance returns 400."""
        api_key, headers = _register_agent(ledger_client, "wd-insufficient")
        _fund_agent(withdraw_session_factory, "wd-insufficient", "1.0")

        dest = generate_test_monero_address(network_byte=24)
        resp = ledger_client.post(
            "/v2/balance/withdraw",
            json={"amount": 5.0, "address": dest},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "insufficient" in resp.json()["detail"].lower()

    def test_withdraw_no_auth_returns_401(self, ledger_client):
        """Request without auth header returns 401."""
        dest = generate_test_monero_address(network_byte=24)
        resp = ledger_client.post(
            "/v2/balance/withdraw",
            json={"amount": 1.0, "address": dest},
        )
        assert resp.status_code == 401

    def test_withdraw_invalid_api_key_returns_401(self, ledger_client):
        """Request with invalid API key returns 401."""
        dest = generate_test_monero_address(network_byte=24)
        resp = ledger_client.post(
            "/v2/balance/withdraw",
            json={"amount": 1.0, "address": dest},
            headers={"Authorization": "Bearer sk_invalid_key_here"},
        )
        assert resp.status_code == 401

    def test_withdraw_below_minimum_returns_422(self, ledger_client, withdraw_session_factory):
        """Withdrawal below minimum amount (0.001 XMR) returns validation error."""
        api_key, headers = _register_agent(ledger_client, "wd-min")
        _fund_agent(withdraw_session_factory, "wd-min", "10.0")

        dest = generate_test_monero_address(network_byte=24)
        resp = ledger_client.post(
            "/v2/balance/withdraw",
            json={"amount": 0.0001, "address": dest},
            headers=headers,
        )
        assert resp.status_code == 422

    def test_withdraw_zero_balance(self, ledger_client):
        """Withdrawal with zero balance returns 400."""
        api_key, headers = _register_agent(ledger_client, "wd-zero")

        dest = generate_test_monero_address(network_byte=24)
        resp = ledger_client.post(
            "/v2/balance/withdraw",
            json={"amount": 1.0, "address": dest},
            headers=headers,
        )
        assert resp.status_code == 400

    def test_withdraw_missing_address_returns_422(self, ledger_client, withdraw_session_factory):
        """Withdrawal without address returns validation error."""
        api_key, headers = _register_agent(ledger_client, "wd-noaddr")
        _fund_agent(withdraw_session_factory, "wd-noaddr", "10.0")

        resp = ledger_client.post(
            "/v2/balance/withdraw",
            json={"amount": 1.0},
            headers=headers,
        )
        assert resp.status_code == 422

    def test_withdraw_idempotency(self, ledger_client, withdraw_session_factory):
        """Same idempotency key returns cached response, not double-deduct."""
        api_key, headers = _register_agent(ledger_client, "wd-idemp")
        _fund_agent(withdraw_session_factory, "wd-idemp", "10.0")

        dest = generate_test_monero_address(network_byte=24)
        idem_key = "test-idempotency-key-12345678"

        resp1 = ledger_client.post(
            "/v2/balance/withdraw",
            json={"amount": 2.0, "address": dest},
            headers={**headers, "idempotency-key": idem_key},
        )
        assert resp1.status_code == 200

        resp2 = ledger_client.post(
            "/v2/balance/withdraw",
            json={"amount": 2.0, "address": dest},
            headers={**headers, "idempotency-key": idem_key},
        )
        assert resp2.status_code == 200
        assert resp2.json() == resp1.json()

        # Balance should only be deducted once
        bal_resp = ledger_client.get("/v2/balance", headers=headers)
        available = Decimal(bal_resp.json()["available"])
        assert available == Decimal("8.0") or available == Decimal("8.000000000000")

    def test_withdraw_multiple_sequential(self, ledger_client, withdraw_session_factory):
        """Multiple sequential withdrawals correctly track running balance."""
        api_key, headers = _register_agent(ledger_client, "wd-multi")
        _fund_agent(withdraw_session_factory, "wd-multi", "10.0")

        dest = generate_test_monero_address(network_byte=24)

        for i in range(3):
            resp = ledger_client.post(
                "/v2/balance/withdraw",
                json={"amount": 2.0, "address": dest},
                headers=headers,
            )
            assert resp.status_code == 200, f"Withdrawal {i+1} failed: {resp.text}"

        bal_resp = ledger_client.get("/v2/balance", headers=headers)
        available = Decimal(bal_resp.json()["available"])
        assert available == Decimal("4.0") or available == Decimal("4.000000000000")
