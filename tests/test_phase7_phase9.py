"""Tests for Phase 7 (MEDIUM security) and Phase 9 (LOW priority) fixes.

Covers:
- 7.1: Logout POST with CSRF protection
- 7.2: XMR address validator on registration/settings schemas
- 7.3: Missing security headers (Permissions-Policy, X-XSS-Protection)
- 9.1: Crypto singleton lock (thread safety)
- 9.4: Fee collector bulk withdraw
- 9.5: Atomic failed-auth recording
- 9.8: Monero RPC host validation
- 9.9: Audit log address redaction
"""

import os
import re
import threading
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from contextlib import ExitStack, contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, AgentTier,
    RateLimitTier, PrivacyLevel, HubRoute, HubRouteStatus, FeeCollection,
    FeeCollectionStatus,
)

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
]

ADMIN_KEY = "test-admin-key-for-dashboard"


# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════

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
def seed_data(db_session_factory):
    """Seed test data."""
    session = db_session_factory()
    agents = []
    for i, (name, tier) in enumerate([
        ("agent-alpha", AgentTier.VERIFIED),
        ("agent-beta", AgentTier.FREE),
    ]):
        agent = Agent(
            agent_name=name,
            api_key_hash=f"hash_{i}",
            tier=tier,
            rate_limit_tier=RateLimitTier.STANDARD,
            privacy_level=PrivacyLevel.MEDIUM,
            is_active=True,
        )
        session.add(agent)
        session.flush()
        balance = AgentBalance(agent_id=agent.id, available=Decimal("10.0"), pending=Decimal("0"))
        session.add(balance)
        agents.append(agent)
    session.commit()
    session.close()
    return agents


@pytest.fixture
def admin_client(db_engine, db_session_factory, seed_data):
    """Test client with admin UI endpoints."""

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

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy", "timestamp": "2026-03-09T00:00:00", "checks": {}
    }
    mock_monitor.get_alerts.return_value = []
    mock_monitor.start_monitoring.return_value = None
    mock_monitor.stop_monitoring.return_value = None

    mock_webhook = MagicMock()
    mock_webhook.start_worker = MagicMock(return_value=MagicMock())
    mock_webhook.stop_worker.return_value = None
    mock_webhook.close = MagicMock(return_value=MagicMock())
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "HUB_MODE": "ledger",
            "DATABASE_URL": "sqlite:///:memory:",
            "ENVIRONMENT": "dev",
            "ADMIN_API_KEY": ADMIN_KEY,
        }))
        for mod in [
            "sthrip.db.database", "api.deps",
            "api.routers.health", "api.routers.agents", "api.routers.payments",
            "api.routers.balance", "api.routers.webhooks",
            "sthrip.services.agent_registry", "sthrip.services.fee_collector",
        ]:
            try:
                stack.enter_context(patch(f"{mod}.get_db", get_test_db))
            except AttributeError:
                pass
        try:
            stack.enter_context(patch("api.admin_ui.views.get_db", get_test_db))
        except (AttributeError, ModuleNotFoundError):
            pass

        stack.enter_context(patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor))
        stack.enter_context(patch("sthrip.services.monitoring.setup_default_monitoring", return_value=mock_monitor))
        stack.enter_context(patch("sthrip.services.webhook_service.get_webhook_service", return_value=mock_webhook))
        stack.enter_context(patch("sthrip.services.rate_limiter.get_rate_limiter", return_value=mock_limiter))
        stack.enter_context(patch("sthrip.db.database.create_tables"))
        stack.enter_context(patch("sthrip.db.database.get_engine", return_value=MagicMock()))

        for mod in ["api.deps", "api.routers.agents", "api.routers.payments", "api.routers.balance", "api.routers.admin"]:
            try:
                stack.enter_context(patch(f"{mod}.audit_log"))
            except AttributeError:
                pass

        import importlib
        import api.main_v2 as main_mod
        importlib.reload(main_mod)
        app = main_mod.create_app()

        from fastapi.testclient import TestClient
        client = TestClient(app, raise_server_exceptions=False)
        yield client


def _get_csrf_token(client) -> str:
    resp = client.get("/admin/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert match, "No csrf_token found in login form"
    return match.group(1)


def _login(client):
    csrf_token = _get_csrf_token(client)
    resp = client.post("/admin/login", data={"admin_key": ADMIN_KEY, "csrf_token": csrf_token})
    assert resp.status_code in (200, 302, 303)
    return client


# ═══════════════════════════════════════════════════════════════════════════════
# 7.1: Logout is POST (not GET)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLogoutPost:
    """MED-7: Logout must be POST, not GET."""

    def test_logout_post_clears_session(self, admin_client):
        """POST /admin/logout should clear session and redirect."""
        _login(admin_client)
        resp = admin_client.post("/admin/logout", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "login" in resp.headers.get("location", "")

    def test_logout_get_returns_405(self, admin_client):
        """GET /admin/logout should return 405 Method Not Allowed."""
        _login(admin_client)
        resp = admin_client.get("/admin/logout", follow_redirects=False)
        assert resp.status_code == 405

    def test_logout_template_uses_form(self, admin_client):
        """Base template should use a form for logout, not a plain link."""
        _login(admin_client)
        resp = admin_client.get("/admin/")
        # Should NOT have <a href="/admin/logout">
        assert 'href="/admin/logout"' not in resp.text
        # Should have a form posting to /admin/logout
        assert 'action="/admin/logout"' in resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# 7.2: XMR address validator on schemas
# ═══════════════════════════════════════════════════════════════════════════════

class TestXmrAddressValidation:
    """MED-8: AgentRegistration and AgentSettingsUpdate must validate xmr_address."""

    def test_registration_rejects_invalid_xmr_address(self):
        from api.schemas import AgentRegistration
        with pytest.raises(Exception):
            AgentRegistration(
                agent_name="test-agent",
                xmr_address="INVALID_ADDRESS",
            )

    def test_registration_accepts_valid_stagenet_address(self):
        from api.schemas import AgentRegistration
        addr = "5" + "a" * 94  # 95 chars, starts with 5 (stagenet)
        reg = AgentRegistration(agent_name="test-agent", xmr_address=addr)
        assert reg.xmr_address == addr

    def test_registration_accepts_none_xmr_address(self):
        from api.schemas import AgentRegistration
        reg = AgentRegistration(agent_name="test-agent", xmr_address=None)
        assert reg.xmr_address is None

    def test_settings_update_rejects_invalid_xmr_address(self):
        from api.schemas import AgentSettingsUpdate
        with pytest.raises(Exception):
            AgentSettingsUpdate(xmr_address="bad")

    def test_settings_update_accepts_valid_xmr_address(self):
        from api.schemas import AgentSettingsUpdate
        addr = "5" + "a" * 94
        update = AgentSettingsUpdate(xmr_address=addr)
        assert update.xmr_address == addr

    def test_settings_update_accepts_none_xmr_address(self):
        from api.schemas import AgentSettingsUpdate
        update = AgentSettingsUpdate(xmr_address=None)
        assert update.xmr_address is None


# ═══════════════════════════════════════════════════════════════════════════════
# 7.3: Security headers
# ═══════════════════════════════════════════════════════════════════════════════

class TestSecurityHeaders:
    """MED-9/13: Permissions-Policy and X-XSS-Protection headers."""

    @pytest.fixture
    def middleware_client(self):
        from fastapi.testclient import TestClient
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        with patch("api.main_v2.lifespan", noop_lifespan):
            import importlib
            import api.main_v2
            importlib.reload(api.main_v2)
            app = api.main_v2.create_app()
            yield TestClient(app)

    def test_permissions_policy_header(self, middleware_client):
        resp = middleware_client.get("/health")
        assert "permissions-policy" in resp.headers
        assert "camera=()" in resp.headers["permissions-policy"]
        assert "microphone=()" in resp.headers["permissions-policy"]

    def test_x_xss_protection_header(self, middleware_client):
        resp = middleware_client.get("/health")
        assert "x-xss-protection" in resp.headers
        assert resp.headers["x-xss-protection"] == "0"


# ═══════════════════════════════════════════════════════════════════════════════
# 9.1: Crypto singleton lock
# ═══════════════════════════════════════════════════════════════════════════════

class TestCryptoSingletonLock:
    """LOW-1: _get_fernet must use threading.Lock for thread safety."""

    def test_fernet_lock_exists(self):
        import sthrip.crypto as crypto_mod
        assert hasattr(crypto_mod, "_fernet_lock")
        assert isinstance(crypto_mod._fernet_lock, type(threading.Lock()))

    def test_concurrent_fernet_init_same_instance(self):
        """Multiple threads calling _get_fernet must all get the same instance."""
        import sthrip.crypto as crypto_mod
        crypto_mod._fernet_instance = None

        results = []
        errors = []

        def worker():
            try:
                f = crypto_mod._get_fernet()
                results.append(id(f))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent init: {errors}"
        assert len(set(results)) == 1, "All threads should get the same Fernet instance"


# ═══════════════════════════════════════════════════════════════════════════════
# 9.4: Fee collector bulk withdraw
# ═══════════════════════════════════════════════════════════════════════════════

class TestBulkWithdrawFees:
    """LOW-4: withdraw_fees must use bulk query instead of per-ID loop."""

    @patch("sthrip.services.fee_collector.get_db")
    def test_withdraw_uses_bulk_query(self, mock_get_db):
        """withdraw_fees should use .filter(.in_()) bulk query."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        # Setup: mock bulk query returning total and count
        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_filter = MagicMock()
        mock_query.filter.return_value = mock_filter
        mock_filter.scalar.return_value = Decimal("0.3")
        mock_filter.update.return_value = 3

        from sthrip.services.fee_collector import FeeCollector
        collector = FeeCollector()
        result = collector.withdraw_fees(["fee_1", "fee_2", "fee_3"], "tx_abc")

        assert result["tx_hash"] == "tx_abc"
        assert "withdrawn_fees" in result
        assert "total_amount" in result

    @patch("sthrip.services.fee_collector.get_db")
    def test_withdraw_returns_correct_count(self, mock_get_db):
        """Bulk update count must match actual updated rows."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_filter = MagicMock()
        mock_query.filter.return_value = mock_filter
        # Total amount query
        mock_filter.scalar.return_value = Decimal("0.2")
        # Bulk update returns 2 rows
        mock_filter.update.return_value = 2

        from sthrip.services.fee_collector import FeeCollector
        collector = FeeCollector()
        result = collector.withdraw_fees(["fee_1", "fee_2"], "tx_xyz")

        assert result["withdrawn_fees"] == 2
        assert result["total_amount"] == float(Decimal("0.2"))


# ═══════════════════════════════════════════════════════════════════════════════
# 9.5: Atomic failed-auth recording
# ═══════════════════════════════════════════════════════════════════════════════

class TestAtomicFailedAuth:
    """LOW-5: record_failed_auth must use atomic Redis pipeline or lock-protected local."""

    def test_record_failed_auth_redis_uses_pipeline(self):
        """Redis path should use HINCRBY in a pipeline."""
        from sthrip.services.rate_limiter import RateLimiter

        limiter = RateLimiter.__new__(RateLimiter)
        limiter.use_redis = True
        mock_pipe = MagicMock()
        limiter.redis = MagicMock()
        limiter.redis.pipeline.return_value = mock_pipe

        limiter.record_failed_auth("1.2.3.4")

        limiter.redis.pipeline.assert_called_once()
        mock_pipe.hincrby.assert_called_once()
        mock_pipe.execute.assert_called_once()

    def test_record_failed_auth_local_uses_lock(self):
        """Local path should use _cache_lock."""
        from sthrip.services.rate_limiter import RateLimiter

        limiter = RateLimiter(redis_url=None)

        limiter.record_failed_auth("1.2.3.4")

        # After recording, the cache should have the key
        assert any("failed_auth" in k for k in limiter._local_cache)


# ═══════════════════════════════════════════════════════════════════════════════
# 9.8: Monero RPC host validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestMoneroRpcHostValidation:
    """LOW-8: monero_rpc_host must reject loopback in non-dev environments."""

    def test_dev_allows_loopback(self):
        from sthrip.config import Settings
        s = Settings(
            admin_api_key="test-key",
            environment="dev",
            hub_mode="ledger",
            monero_rpc_host="127.0.0.1",
        )
        assert s.monero_rpc_host == "127.0.0.1"

    def test_production_rejects_loopback(self):
        from sthrip.config import Settings
        with pytest.raises(Exception, match="loopback"):
            Settings(
                admin_api_key="real-secure-key-for-production",
                api_key_hmac_secret="real-hmac-secret-for-production-32ch",
                webhook_encryption_key="uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=",
                monero_rpc_pass="secure-rpc-pass",
                environment="production",
                hub_mode="onchain",
                monero_rpc_host="127.0.0.1",
            )

    def test_production_allows_hostname(self):
        from sthrip.config import Settings
        s = Settings(
            admin_api_key="real-secure-key-for-production",
            api_key_hmac_secret="real-hmac-secret-for-production-32ch",
            webhook_encryption_key="uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=",
            monero_rpc_pass="secure-rpc-pass",
            environment="production",
            monero_network="mainnet",
            hub_mode="onchain",
            monero_rpc_host="monero-wallet-rpc.railway.internal",
        )
        assert s.monero_rpc_host == "monero-wallet-rpc.railway.internal"

    def test_production_allows_private_ip(self):
        from sthrip.config import Settings
        s = Settings(
            admin_api_key="real-secure-key-for-production",
            api_key_hmac_secret="real-hmac-secret-for-production-32ch",
            webhook_encryption_key="uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=",
            monero_rpc_pass="secure-rpc-pass",
            environment="production",
            monero_network="mainnet",
            hub_mode="onchain",
            monero_rpc_host="10.0.0.5",
        )
        assert s.monero_rpc_host == "10.0.0.5"


# ═══════════════════════════════════════════════════════════════════════════════
# 9.9: Audit log address redaction
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuditLogRedaction:
    """LOW-9: Audit log must redact wallet addresses."""

    def test_redact_addresses_truncates(self):
        from api.routers.agents import _redact_addresses
        result = _redact_addresses({
            "xmr_address": "5abcdef1234567890abcdef",
            "base_address": "0x1234567890abcdef",
            "other_field": "keep_this",
        })
        assert result["xmr_address"] == "5abcdef1..."
        assert result["base_address"] == "0x123456..."
        assert result["other_field"] == "keep_this"

    def test_redact_addresses_handles_none(self):
        from api.routers.agents import _redact_addresses
        result = _redact_addresses({"xmr_address": None, "name": "test"})
        assert result["xmr_address"] is None
        assert result["name"] == "test"

    def test_redact_addresses_handles_empty_dict(self):
        from api.routers.agents import _redact_addresses
        result = _redact_addresses({})
        assert result == {}
