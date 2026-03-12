"""Tests for Phase 6 — HIGH Access Control (HIGH-10 through HIGH-13)."""

import os
import pytest
from unittest.mock import patch, MagicMock
from contextlib import contextmanager, ExitStack
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, AgentTier,
    RateLimitTier, PrivacyLevel, HubRoute, FeeCollection, PendingWithdrawal,
)
from sthrip.services.rate_limiter import RateLimitExceeded

# Valid-looking stagenet XMR address (starts with "5", 95 chars, base58 chars)
_FAKE_XMR_ADDR = "5" + "A" * 94

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    PendingWithdrawal.__table__,
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
    """FastAPI test client with mocked dependencies."""

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
        "status": "healthy", "timestamp": "2026-03-03T00:00:00", "checks": {}
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger"}))
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
            patch(
                "sthrip.services.monitoring.get_monitor",
                return_value=mock_monitor,
            )
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

        import importlib
        import api.main_v2
        importlib.reload(api.main_v2)
        from api.main_v2 import app

        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def rate_limited_client(db_engine, db_session_factory):
    """Client where the rate limiter raises RateLimitExceeded for discovery."""

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

    import time
    import importlib

    mock_limiter = MagicMock()
    mock_limiter.check_rate_limit.return_value = None
    mock_limiter.check_ip_rate_limit.side_effect = RateLimitExceeded(
        limit=60, reset_at=time.time() + 30
    )
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 0}
    mock_limiter.use_redis = False
    mock_limiter._cache_lock = __import__("threading").Lock()
    mock_limiter._local_cache = {}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy", "timestamp": "2026-03-03T00:00:00", "checks": {}
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger"}))
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
            patch(
                "sthrip.services.monitoring.get_monitor",
                return_value=mock_monitor,
            )
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

        import importlib
        import api.main_v2
        importlib.reload(api.main_v2)
        from api.main_v2 import app

        yield TestClient(app, raise_server_exceptions=False)


# ═══════════════════════════════════════════════════════════════════════════════
# HIGH-10: Rate-limit discovery endpoints
# ═══════════════════════════════════════════════════════════════════════════════


class TestDiscoveryRateLimiting:
    """Rate limiting on discovery endpoints: verify code presence + integration test."""

    def test_get_agent_profile_has_rate_limiting(self):
        """Verify rate limiting code is present in get_agent_profile."""
        import inspect
        from api.routers.agents import get_agent_profile
        src = inspect.getsource(get_agent_profile)
        assert "_check_ip_rate_limit" in src

    def test_discover_agents_has_rate_limiting(self):
        """Verify rate limiting code is present in discover_agents."""
        import inspect
        from api.routers.agents import discover_agents
        src = inspect.getsource(discover_agents)
        assert "check_ip_rate_limit" in src

    def test_get_leaderboard_has_rate_limiting(self):
        """Verify rate limiting code is present in get_leaderboard."""
        import inspect
        from api.routers.agents import get_leaderboard
        src = inspect.getsource(get_leaderboard)
        assert "check_ip_rate_limit" in src

    def test_discovery_allowed_when_not_rate_limited(self, client):
        """Normal client should not get 429."""
        r = client.get("/v2/agents")
        assert r.status_code == 200

    def test_leaderboard_allowed_when_not_rate_limited(self, client):
        r = client.get("/v2/leaderboard")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# HIGH-10 (privacy): Hide xmr_address for high/paranoid privacy
# ═══════════════════════════════════════════════════════════════════════════════


class TestPrivacyGating:
    """xmr_address should be hidden for agents with high or paranoid privacy."""

    def test_xmr_address_visible_for_low_privacy(self, client):
        reg = client.post("/v2/agents/register", json={
            "agent_name": "low-privacy-agent",
            "xmr_address": _FAKE_XMR_ADDR,
            "privacy_level": "low",
        })
        assert reg.status_code == 201, f"Registration failed: {reg.text}"
        r = client.get("/v2/agents/low-privacy-agent")
        assert r.status_code == 200
        assert r.json()["xmr_address"] == _FAKE_XMR_ADDR

    def test_xmr_address_visible_for_medium_privacy(self, client):
        client.post("/v2/agents/register", json={
            "agent_name": "medium-privacy-agent",
            "xmr_address": _FAKE_XMR_ADDR,
            "privacy_level": "medium",
        })
        r = client.get("/v2/agents/medium-privacy-agent")
        assert r.status_code == 200
        assert r.json()["xmr_address"] == _FAKE_XMR_ADDR

    def test_xmr_address_hidden_for_high_privacy(self, client):
        client.post("/v2/agents/register", json={
            "agent_name": "high-privacy-agent",
            "xmr_address": _FAKE_XMR_ADDR,
            "privacy_level": "high",
        })
        r = client.get("/v2/agents/high-privacy-agent")
        assert r.status_code == 200
        assert r.json()["xmr_address"] is None

    def test_xmr_address_hidden_for_paranoid_privacy(self, client):
        client.post("/v2/agents/register", json={
            "agent_name": "paranoid-privacy-agent",
            "xmr_address": _FAKE_XMR_ADDR,
            "privacy_level": "paranoid",
        })
        r = client.get("/v2/agents/paranoid-privacy-agent")
        assert r.status_code == 200
        assert r.json()["xmr_address"] is None

    def test_xmr_address_hidden_in_discover_list(self, client):
        """discover_agents should also hide xmr_address for high/paranoid."""
        client.post("/v2/agents/register", json={
            "agent_name": "hidden-in-list",
            "xmr_address": _FAKE_XMR_ADDR,
            "privacy_level": "high",
        })
        r = client.get("/v2/agents")
        assert r.status_code == 200
        data = r.json()
        agents = data["items"] if isinstance(data, dict) else data
        hidden = [a for a in agents if a["agent_name"] == "hidden-in-list"]
        assert len(hidden) == 1
        assert hidden[0]["xmr_address"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# HIGH-11: Restrict query_key to view_key only
# ═══════════════════════════════════════════════════════════════════════════════


class TestQueryKeyRestriction:
    """query_key should only allow view_key."""

    def test_query_key_view_key_allowed(self):
        from sthrip.wallet import MoneroWalletRPC

        wallet = MoneroWalletRPC()
        with patch.object(wallet, "_call", return_value={"key": "abc123"}) as mock_call:
            result = wallet.query_key("view_key")
            mock_call.assert_called_once_with("query_key", {"key_type": "view_key"})
            assert result == {"key": "abc123"}

    def test_query_key_spend_key_blocked(self):
        from sthrip.wallet import MoneroWalletRPC

        wallet = MoneroWalletRPC()
        with pytest.raises(ValueError, match="only allows"):
            wallet.query_key("spend_key")

    def test_query_key_mnemonic_blocked(self):
        from sthrip.wallet import MoneroWalletRPC

        wallet = MoneroWalletRPC()
        with pytest.raises(ValueError, match="only allows"):
            wallet.query_key("mnemonic")

    def test_query_key_arbitrary_blocked(self):
        from sthrip.wallet import MoneroWalletRPC

        wallet = MoneroWalletRPC()
        with pytest.raises(ValueError, match="only allows"):
            wallet.query_key("evil_key_type")


# ═══════════════════════════════════════════════════════════════════════════════
# HIGH-12: Validate transfer_type before JSON-RPC
# ═══════════════════════════════════════════════════════════════════════════════


class TestTransferTypeValidation:
    """incoming_transfers should validate transfer_type."""

    def test_incoming_transfers_all_allowed(self):
        from sthrip.wallet import MoneroWalletRPC

        wallet = MoneroWalletRPC()
        with patch.object(wallet, "_call", return_value={"transfers": []}) as mock:
            wallet.incoming_transfers(transfer_type="all")
            mock.assert_called_once()

    def test_incoming_transfers_available_allowed(self):
        from sthrip.wallet import MoneroWalletRPC

        wallet = MoneroWalletRPC()
        with patch.object(wallet, "_call", return_value={"transfers": []}) as mock:
            wallet.incoming_transfers(transfer_type="available")
            mock.assert_called_once()

    def test_incoming_transfers_unavailable_allowed(self):
        from sthrip.wallet import MoneroWalletRPC

        wallet = MoneroWalletRPC()
        with patch.object(wallet, "_call", return_value={"transfers": []}) as mock:
            wallet.incoming_transfers(transfer_type="unavailable")
            mock.assert_called_once()

    def test_incoming_transfers_invalid_type_rejected(self):
        from sthrip.wallet import MoneroWalletRPC

        wallet = MoneroWalletRPC()
        with pytest.raises(ValueError, match="transfer_type must be one of"):
            wallet.incoming_transfers(transfer_type="evil")

    def test_incoming_transfers_sql_injection_rejected(self):
        from sthrip.wallet import MoneroWalletRPC

        wallet = MoneroWalletRPC()
        with pytest.raises(ValueError, match="transfer_type must be one of"):
            wallet.incoming_transfers(transfer_type="all; DROP TABLE")


# ═══════════════════════════════════════════════════════════════════════════════
# HIGH-13: Fix update_agent_settings session handling
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpdateAgentSettingsSession:
    """update_agent_settings should use injected session, not get_db()."""

    def test_update_settings_basic(self, client):
        """Register, then update settings."""
        r = client.post("/v2/agents/register", json={
            "agent_name": "settings-agent",
            "xmr_address": _FAKE_XMR_ADDR,
        })
        assert r.status_code == 201
        api_key = r.json()["api_key"]

        r2 = client.patch(
            "/v2/me/settings",
            json={"xmr_address": _FAKE_XMR_ADDR},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert r2.status_code == 200
        assert "xmr_address" in r2.json()["updated"]

    def test_update_settings_privacy_level(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "privacy-update-agent",
        })
        assert r.status_code == 201
        api_key = r.json()["api_key"]

        r2 = client.patch(
            "/v2/me/settings",
            json={"privacy_level": "high"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert r2.status_code == 200
        assert "privacy_level" in r2.json()["updated"]

    def test_update_settings_no_fields_returns_400(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "empty-update-agent",
        })
        assert r.status_code == 201
        api_key = r.json()["api_key"]

        r2 = client.patch(
            "/v2/me/settings",
            json={},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert r2.status_code == 400

    def test_update_settings_uses_injected_session(self):
        """Verify update_agent_settings uses Depends(get_db_session), not get_db()."""
        import inspect
        from api.routers.agents import update_agent_settings

        sig = inspect.signature(update_agent_settings)
        param_names = list(sig.parameters.keys())
        assert "db" in param_names, "update_agent_settings should have a 'db' parameter from DI"


class TestAdminKeyHeaderRemoved:
    """Task 7: Raw admin-key header bypass must be removed."""

    def test_admin_key_header_rejected(self, client):
        """admin-key header should no longer grant admin access."""
        r = client.get(
            "/v2/admin/stats",
            headers={"admin-key": "test-admin-key"},
        )
        assert r.status_code == 401, (
            "admin-key header should be rejected — use session tokens only"
        )
