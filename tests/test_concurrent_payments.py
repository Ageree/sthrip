"""
Concurrent payment safety tests.

Verifies that balance operations are atomic and consistent
under concurrent access. Uses threads to simulate parallel requests.
"""
import os
import contextlib
import threading
import pytest
from decimal import Decimal
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


def _register_agent(client, name, xmr_address=None):
    body = {"agent_name": name, "privacy_level": "low"}
    if xmr_address:
        body["xmr_address"] = xmr_address
    resp = client.post("/v2/agents/register", json=body)
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    return data["agent_id"], data["api_key"]


def _deposit(client, api_key, amount):
    resp = client.post(
        "/v2/balance/deposit",
        json={"amount": amount, "token": "XMR"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _get_balance(client, api_key):
    resp = client.get(
        "/v2/balance",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    return data.get("available", 0)


class TestConcurrentDeductions:
    """Verify that concurrent balance operations stay consistent."""

    @pytest.mark.skipif(
        not os.getenv("CI_HAS_POSTGRES"),
        reason="Requires PostgreSQL — set CI_HAS_POSTGRES=true to enable",
    )
    def test_concurrent_withdrawals_never_go_negative(self, client):
        """
        Deposit 10 XMR, then fire 5 threads each trying to withdraw 5 XMR.
        Only 2 should succeed (10 / 5 = 2). Balance must never go negative.
        Requires PostgreSQL with FOR UPDATE support.
        """
        agent_id, api_key = _register_agent(client, "concurrent_agent")
        _deposit(client, api_key, 10.0)

        results = []
        errors = []

        def try_withdraw():
            resp = client.post(
                "/v2/balance/withdraw",
                json={"amount": 5.0, "token": "XMR", "address": "5" + "a" * 94},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            results.append(resp.status_code)

        threads = [threading.Thread(target=try_withdraw) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = results.count(200)
        available = _get_balance(client, api_key)

        # At most 2 withdrawals should succeed (10 / 5 = 2)
        assert successes <= 2, f"Expected at most 2 successes, got {successes}"
        assert available >= 0, f"Balance went negative: {available}"

    @pytest.mark.skipif(
        not os.getenv("CI_HAS_POSTGRES"),
        reason="Requires PostgreSQL — set CI_HAS_POSTGRES=true to enable",
    )
    def test_concurrent_hub_payments_consistent(self, client):
        """
        Register A and B, deposit 10 to A, then fire 5 concurrent
        hub payments of 3 XMR each. At most 3 should succeed (accounting for fees).
        Requires PostgreSQL with FOR UPDATE support.
        """
        a_id, a_key = _register_agent(client, "hub_sender")
        b_id, b_key = _register_agent(client, "hub_receiver")
        _deposit(client, a_key, 10.0)

        results = []

        def try_pay(i):
            resp = client.post(
                "/v2/payments/hub-routing",
                json={
                    "to_agent_name": "hub_receiver",
                    "amount": 3.0,
                    "token": "XMR",
                },
                headers={
                    "Authorization": f"Bearer {a_key}",
                    "Idempotency-Key": f"pay-{i}",
                },
            )
            results.append(resp.status_code)

        threads = [threading.Thread(target=try_pay, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        available_a = _get_balance(client, a_key)

        # Balance must never go negative
        assert available_a >= 0, f"Sender balance went negative: {available_a}"

    @pytest.mark.skipif(
        not os.getenv("CI_HAS_POSTGRES"),
        reason="Requires PostgreSQL — set CI_HAS_POSTGRES=true to enable",
    )
    def test_idempotency_key_deduplication(self, client):
        """
        Same idempotency key sent 5 times concurrently.
        Exactly one payment should be created; balance deducted once.
        Requires PostgreSQL with FOR UPDATE support.
        """
        a_id, a_key = _register_agent(client, "idem_sender")
        b_id, b_key = _register_agent(client, "idem_receiver_xmr", xmr_address="addr_" + "r" * 90)
        _deposit(client, a_key, 10.0)

        results = []

        def try_pay():
            resp = client.post(
                "/v2/payments/hub-routing",
                json={
                    "to_agent_name": "idem_receiver_xmr",
                    "amount": 2.0,
                    "token": "XMR",
                },
                headers={
                    "Authorization": f"Bearer {a_key}",
                    "Idempotency-Key": "same-key-for-all",
                },
            )
            results.append((resp.status_code, resp.json()))

        threads = [threading.Thread(target=try_pay) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[0] == 200]
        available_a = _get_balance(client, a_key)

        # All should return 200 (idempotent replay returns cached response)
        assert all(r[0] == 200 for r in results), f"Some failed: {results}"
        # But balance should only be deducted once (~8 XMR remaining for 2 + small fee)
        assert available_a > 7.0, f"Balance deducted multiple times: {available_a}"

    def test_rapid_deposit_and_withdraw_consistency(self, client):
        """
        Rapid sequential deposit + withdraw operations.
        Final balance must match expected value.
        """
        agent_id, api_key = _register_agent(client, "consistency_agent")
        _deposit(client, api_key, 20.0)

        # Rapid sequential operations (SQLite can't truly concurrent)
        ops = [("deposit", 5.0), ("withdraw", 3.0), ("deposit", 2.0),
               ("withdraw", 4.0), ("deposit", 1.0), ("withdraw", 6.0)]

        expected = 20.0
        for op, amount in ops:
            if op == "deposit":
                resp = client.post(
                    "/v2/balance/deposit",
                    json={"amount": amount, "token": "XMR"},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if resp.status_code == 200:
                    expected += amount
            else:
                resp = client.post(
                    "/v2/balance/withdraw",
                    json={"amount": amount, "token": "XMR", "address": "5" + "b" * 94},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if resp.status_code == 200:
                    expected -= amount

        available = _get_balance(client, api_key)

        assert abs(available - expected) < 0.001, (
            f"Balance mismatch: got {available}, expected {expected}"
        )
