"""Tests for on-chain deposit/withdraw endpoints — TDD RED phase"""
import os
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from contextlib import contextmanager

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, HubRoute,
    FeeCollection, Transaction,
    AgentTier, RateLimitTier, PrivacyLevel,
)
from sthrip.db.repository import BalanceRepository
from sthrip.wallet import WalletRPCError

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
    svc.get_or_create_deposit_address.return_value = "5FakeDepositSubaddr"
    svc.send_withdrawal.return_value = {
        "tx_hash": "abc123withdrawal",
        "fee": Decimal("0.00005"),
        "amount": Decimal("3.0"),
    }
    svc.get_wallet_info.return_value = {
        "balance": Decimal("100"),
        "unlocked_balance": Decimal("95"),
        "address": "5HubPrimaryAddr",
    }
    return svc


@pytest.fixture
def onchain_client(db_engine, db_session_factory, mock_wallet_service):
    """FastAPI test client in onchain mode."""

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

    from contextlib import ExitStack
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "onchain", "MONERO_NETWORK": "stagenet", "MONERO_MIN_CONFIRMATIONS": "10"}))
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
        stack.enter_context(patch("sthrip.services.webhook_service.get_webhook_service", return_value=mock_webhook))
        stack.enter_context(patch("sthrip.services.webhook_service.queue_webhook"))
        stack.enter_context(patch("api.helpers.get_wallet_service", return_value=mock_wallet_service))
        stack.enter_context(patch("api.routers.balance.get_wallet_service", return_value=mock_wallet_service))
        for mod in ["api.deps", "api.routers.agents", "api.routers.payments", "api.routers.balance", "api.routers.admin", "api.main_v2"]:
            stack.enter_context(patch(f"{mod}.audit_log"))

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def registered_agent(onchain_client):
    r = onchain_client.post("/v2/agents/register", json={
        "agent_name": "onchain-agent",
        "xmr_address": "test_xmr_address",
    })
    assert r.status_code == 201, f"Registration failed: {r.text}"
    return r.json()["api_key"], "onchain-agent"


# ═══════════════════════════════════════════════════════════════════════════════
# DEPOSIT — ONCHAIN MODE
# ═══════════════════════════════════════════════════════════════════════════════

class TestDepositOnchain:
    def test_deposit_returns_subaddress(self, onchain_client, registered_agent):
        key, _ = registered_agent
        r = onchain_client.post(
            "/v2/balance/deposit",
            json={},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert "deposit_address" in data
        assert data["deposit_address"] == "5FakeDepositSubaddr"
        assert data["token"] == "XMR"
        assert data["network"] == "stagenet"
        assert data["min_confirmations"] == 10

    def test_deposit_does_not_auto_credit(self, onchain_client, registered_agent):
        key, _ = registered_agent
        onchain_client.post(
            "/v2/balance/deposit",
            json={},
            headers={"Authorization": f"Bearer {key}"},
        )
        r = onchain_client.get(
            "/v2/balance",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.json()["available"] == 0

    def test_deposit_message_included(self, onchain_client, registered_agent):
        key, _ = registered_agent
        r = onchain_client.post(
            "/v2/balance/deposit",
            json={},
            headers={"Authorization": f"Bearer {key}"},
        )
        data = r.json()
        assert "message" in data
        assert "confirmations" in data["message"].lower()


# ═══════════════════════════════════════════════════════════════════════════════
# WITHDRAW — ONCHAIN MODE
# ═══════════════════════════════════════════════════════════════════════════════

class TestWithdrawOnchain:
    def _fund_agent(self, onchain_client, key, db_session_factory):
        """Directly deposit balance for testing withdrawals."""
        from sthrip.db.repository import AgentRepository
        with contextmanager(lambda: (yield db_session_factory()))() as db:
            agent_repo = AgentRepository(db)
            # Find agent by iterating (since we can't easily look up by key in test)
            agent = db.query(Agent).filter(Agent.agent_name == "onchain-agent").first()
            if agent:
                repo = BalanceRepository(db)
                repo.deposit(agent.id, Decimal("10.0"))
                db.commit()

    def test_withdraw_returns_tx_hash(self, onchain_client, registered_agent, db_session_factory):
        key, _ = registered_agent
        self._fund_agent(onchain_client, key, db_session_factory)

        r = onchain_client.post(
            "/v2/balance/withdraw",
            json={"amount": 3.0, "address": "5" + "d" * 94},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "sent"
        assert data["tx_hash"] == "abc123withdrawal"
        assert data["amount"] == 3.0
        assert "fee" in data
        assert data["token"] == "XMR"

    def test_withdraw_insufficient_balance(self, onchain_client, registered_agent):
        key, _ = registered_agent
        r = onchain_client.post(
            "/v2/balance/withdraw",
            json={"amount": 100.0, "address": "5" + "d" * 94},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 400
        assert "Insufficient" in r.json()["detail"]

    def test_withdraw_rpc_failure_rolls_back(
        self, onchain_client, registered_agent, mock_wallet_service, db_session_factory
    ):
        key, _ = registered_agent
        self._fund_agent(onchain_client, key, db_session_factory)

        mock_wallet_service.send_withdrawal.side_effect = WalletRPCError("RPC down")

        r = onchain_client.post(
            "/v2/balance/withdraw",
            json={"amount": 3.0, "address": "5" + "d" * 94},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 502

        # Balance should be restored
        r = onchain_client.get(
            "/v2/balance",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.json()["available"] == 10.0

    def test_withdraw_updates_total_withdrawn(
        self, onchain_client, registered_agent, db_session_factory
    ):
        key, _ = registered_agent
        self._fund_agent(onchain_client, key, db_session_factory)

        onchain_client.post(
            "/v2/balance/withdraw",
            json={"amount": 2.0, "address": "5" + "d" * 94},
            headers={"Authorization": f"Bearer {key}"},
        )
        r = onchain_client.get(
            "/v2/balance",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.json()["total_withdrawn"] == 2.0


# ═══════════════════════════════════════════════════════════════════════════════
# LEDGER MODE — BACKWARD COMPATIBILITY
# ═══════════════════════════════════════════════════════════════════════════════

class TestLedgerModeBackwardCompat:
    """Ensure HUB_MODE=ledger preserves old auto-credit behavior."""

    @pytest.fixture
    def ledger_client(self, db_engine, db_session_factory):
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

        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger"}))
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
            for mod in ["sthrip.services.rate_limiter", "api.deps", "api.routers.agents", "api.main_v2"]:
                stack.enter_context(patch(f"{mod}.get_rate_limiter", return_value=mock_limiter))
            stack.enter_context(patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor))
            stack.enter_context(patch("sthrip.services.monitoring.setup_default_monitoring", return_value=mock_monitor))
            stack.enter_context(patch("sthrip.services.webhook_service.get_webhook_service", return_value=mock_webhook))
            stack.enter_context(patch("sthrip.services.webhook_service.queue_webhook"))
            for mod in ["api.deps", "api.routers.agents", "api.routers.payments", "api.routers.balance", "api.routers.admin", "api.main_v2"]:
                stack.enter_context(patch(f"{mod}.audit_log"))

            from api.main_v2 import app
            yield TestClient(app, raise_server_exceptions=False)

    def test_ledger_deposit_auto_credits(self, ledger_client):
        r = ledger_client.post("/v2/agents/register", json={
            "agent_name": "ledger-agent",
            "xmr_address": "test_addr",
        })
        key = r.json()["api_key"]

        r = ledger_client.post(
            "/v2/balance/deposit",
            json={"amount": 5.0},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "deposited"
        assert r.json()["new_balance"] == 5.0

    def test_ledger_withdraw_no_rpc(self, ledger_client):
        r = ledger_client.post("/v2/agents/register", json={
            "agent_name": "ledger-withdrawer",
            "xmr_address": "test_addr",
        })
        key = r.json()["api_key"]

        ledger_client.post(
            "/v2/balance/deposit",
            json={"amount": 10.0},
            headers={"Authorization": f"Bearer {key}"},
        )
        r = ledger_client.post(
            "/v2/balance/withdraw",
            json={"amount": 3.0, "address": "5" + "e" * 94},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "withdrawn"
        assert r.json()["remaining_balance"] == 7.0
