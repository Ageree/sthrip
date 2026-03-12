"""
Tests for CRIT-3 fix: readiness endpoint must not block the event loop.

Both the DB check and the wallet RPC call must be wrapped with
asyncio.to_thread so that synchronous I/O does not stall other coroutines.
"""
import asyncio
import os
import contextlib
import pytest
from contextlib import contextmanager
from unittest.mock import patch, MagicMock, AsyncMock, call

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, HubRoute,
    FeeCollection, PendingWithdrawal, Transaction,
)

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    PendingWithdrawal.__table__,
    Transaction.__table__,
]

_GET_DB_MODULES = [
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
]

_RATE_LIMITER_MODULES = [
    "sthrip.services.rate_limiter",
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
]

_AUDIT_LOG_MODULES = [
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.admin",
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


def _make_client(db_session_factory, hub_mode="ledger", extra_patches=None):
    """Create a TestClient with all standard patches applied.

    Returns a context manager yielding the client so callers can add
    their own assertions inside the ``with`` block.
    """
    @contextmanager
    def get_test_db():
        from sqlalchemy.orm import Session
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
        "timestamp": "2026-03-11T00:00:00",
        "checks": {},
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    @contextmanager
    def _build():
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.dict(os.environ, {"HUB_MODE": hub_mode, "MONERO_NETWORK": "stagenet"})
            )

            for mod in _GET_DB_MODULES:
                stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))
            stack.enter_context(patch("sthrip.db.database.create_tables"))

            for mod in _RATE_LIMITER_MODULES:
                stack.enter_context(
                    patch(f"{mod}.get_rate_limiter", return_value=mock_limiter)
                )

            for mod in _AUDIT_LOG_MODULES:
                stack.enter_context(patch(f"{mod}.audit_log"))

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

            if extra_patches:
                for p in extra_patches:
                    stack.enter_context(p)

            from api.main_v2 import app
            yield TestClient(app, raise_server_exceptions=False)

    return _build()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: asyncio.to_thread is used for the DB check
# ─────────────────────────────────────────────────────────────────────────────

class TestReadinessDbNonBlocking:
    """The synchronous DB check must be offloaded via asyncio.to_thread."""

    def test_db_check_uses_asyncio_to_thread(self, db_session_factory):
        """asyncio.to_thread must be called at least once during the DB step."""
        calls_made = []

        original_to_thread = asyncio.to_thread

        async def spy_to_thread(func, *args, **kwargs):
            calls_made.append(func)
            return await original_to_thread(func, *args, **kwargs)

        # Patch asyncio.to_thread at the module where it is called from.
        # After the implementation adds ``import asyncio`` to health.py, the
        # correct patch target is the name bound in that module's namespace.
        with _make_client(db_session_factory, hub_mode="ledger",
                          extra_patches=[
                              patch("asyncio.to_thread", side_effect=spy_to_thread),
                          ]) as client:
            r = client.get("/ready")

        assert r.status_code == 200
        assert len(calls_made) >= 1, (
            "asyncio.to_thread was not called — the DB check blocks the event loop"
        )

    def test_ready_still_returns_ok_with_to_thread(self, db_session_factory):
        """Response is unchanged after wrapping DB check in to_thread."""
        with _make_client(db_session_factory, hub_mode="ledger") as client:
            r = client.get("/ready")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["checks"]["database"] == "ok"

    def test_ready_503_when_db_fails(self, db_session_factory):
        """DB failure is still surfaced as 503 after wrapping in to_thread."""
        def failing_db():
            raise RuntimeError("DB is down")

        with _make_client(db_session_factory, hub_mode="ledger",
                          extra_patches=[
                              patch("api.routers.health.get_db", side_effect=failing_db),
                          ]) as client:
            r = client.get("/ready")

        assert r.status_code == 503
        assert r.json()["checks"]["database"] == "failed"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: asyncio.to_thread is used for the wallet RPC check (onchain mode)
# ─────────────────────────────────────────────────────────────────────────────

class TestReadinessWalletNonBlocking:
    """The synchronous wallet RPC call must be offloaded via asyncio.to_thread."""

    def _make_wallet_mock(self):
        mock_wallet_rpc = MagicMock()
        mock_wallet_rpc.get_height.return_value = {"height": 1000}

        mock_wallet_svc = MagicMock()
        mock_wallet_svc.wallet = mock_wallet_rpc
        return mock_wallet_svc, mock_wallet_rpc

    def test_wallet_check_uses_asyncio_to_thread(self, db_session_factory):
        """asyncio.to_thread must be called for get_height in onchain mode."""
        mock_wallet_svc, mock_wallet_rpc = self._make_wallet_mock()
        calls_made = []

        original_to_thread = asyncio.to_thread

        async def spy_to_thread(func, *args, **kwargs):
            calls_made.append(func)
            return await original_to_thread(func, *args, **kwargs)

        extra = [
            patch("api.helpers.get_wallet_service", return_value=mock_wallet_svc),
            patch("asyncio.to_thread", side_effect=spy_to_thread),
        ]

        with _make_client(db_session_factory, hub_mode="onchain",
                          extra_patches=extra) as client:
            r = client.get("/ready")

        assert r.status_code == 200
        assert r.json()["checks"]["wallet_rpc"] == "ok"
        # to_thread must be called at least twice: once for DB, once for wallet
        assert len(calls_made) >= 2, (
            f"Expected at least 2 asyncio.to_thread calls (db + wallet), got {len(calls_made)}"
        )

    def test_get_height_called_via_thread(self, db_session_factory):
        """get_height must be invoked (possibly inside to_thread) in onchain mode."""
        mock_wallet_svc, mock_wallet_rpc = self._make_wallet_mock()

        extra = [
            patch("api.helpers.get_wallet_service", return_value=mock_wallet_svc),
        ]

        with _make_client(db_session_factory, hub_mode="onchain",
                          extra_patches=extra) as client:
            r = client.get("/ready")

        assert r.status_code == 200
        mock_wallet_rpc.get_height.assert_called_once()

    def test_wallet_rpc_failure_returns_503(self, db_session_factory):
        """Wallet RPC failure in onchain mode must return 503."""
        mock_wallet_rpc = MagicMock()
        mock_wallet_rpc.get_height.side_effect = ConnectionError("wallet RPC unreachable")

        mock_wallet_svc = MagicMock()
        mock_wallet_svc.wallet = mock_wallet_rpc

        extra = [
            patch("api.helpers.get_wallet_service", return_value=mock_wallet_svc),
        ]

        with _make_client(db_session_factory, hub_mode="onchain",
                          extra_patches=extra) as client:
            r = client.get("/ready")

        assert r.status_code == 503
        data = r.json()
        assert data["status"] == "not_ready"
        assert data["checks"]["wallet_rpc"] == "failed"

    def test_onchain_ready_includes_both_checks(self, db_session_factory):
        """In onchain mode both database and wallet_rpc appear in checks."""
        mock_wallet_svc, _ = self._make_wallet_mock()

        extra = [
            patch("api.helpers.get_wallet_service", return_value=mock_wallet_svc),
        ]

        with _make_client(db_session_factory, hub_mode="onchain",
                          extra_patches=extra) as client:
            r = client.get("/ready")

        assert r.status_code == 200
        data = r.json()
        assert data["checks"]["database"] == "ok"
        assert data["checks"]["wallet_rpc"] == "ok"

    def test_ledger_mode_skips_wallet_check(self, db_session_factory):
        """In ledger mode wallet_rpc must NOT appear in the checks dict."""
        with _make_client(db_session_factory, hub_mode="ledger") as client:
            r = client.get("/ready")

        assert r.status_code == 200
        assert "wallet_rpc" not in r.json()["checks"]
