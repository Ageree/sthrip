"""Tests for production review fixes.

Covers:
- CRIT-3: Lazy _SessionStore initialization
- CRIT-4: Wallet RPC HTTPS support
- IMP-1: Consolidated admin session stores (views.py uses deps.py store)
- IMP-3: Throttled update_last_seen
- IMP-5: Concurrent webhook processing
- IMP-6: ApiSession removed, AuditLog uses String for ip_address
- MIN-2: Local rate limiter check-before-increment
- MIN-3: Privacy redaction for all address types
"""

import asyncio
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch, AsyncMock

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# CRIT-3: AdminSessionStore lazy init (must NOT call get_settings at import time)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionStoreLazyInit:
    """AdminSessionStore must use lazy Redis init — not called at __init__ time."""

    def test_session_store_init_does_not_call_get_settings(self):
        """Creating AdminSessionStore should NOT call get_settings()."""
        from api.session_store import AdminSessionStore

        with patch("api.session_store.get_settings") as mock_settings:
            store = AdminSessionStore(key_prefix="admin_session:")
            mock_settings.assert_not_called()

    def test_session_store_lazy_redis_on_first_use(self):
        """Redis connection should happen on first set/get, not __init__."""
        from api.session_store import AdminSessionStore

        with patch("api.session_store.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url=None)
            store = AdminSessionStore(key_prefix="admin_session:")
            # No call yet
            mock_settings.assert_not_called()

            # First use triggers lazy init
            store.set_session("test-token", 60)
            mock_settings.assert_called()

    def test_session_store_works_with_in_memory_fallback(self):
        """Store should work in-memory when Redis is unavailable."""
        from api.session_store import AdminSessionStore

        with patch("api.session_store.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url=None)
            store = AdminSessionStore(key_prefix="admin_session:")

            store.set_session("token123", 3600)
            assert store.get_session("token123") is True
            assert store.get_session("wrong-token") is False

            store.delete_session("token123")
            assert store.get_session("token123") is False

    def test_csrf_token_create_and_verify(self):
        """CSRF tokens should work with lazy init."""
        from api.session_store import AdminSessionStore

        with patch("api.session_store.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url=None)
            store = AdminSessionStore(key_prefix="admin_session:")

            token = store.create_csrf_token()
            assert store.verify_csrf_token(token) is True
            # Single-use: second verify should fail
            assert store.verify_csrf_token(token) is False


# ═══════════════════════════════════════════════════════════════════════════════
# CRIT-4: Wallet RPC HTTPS support
# ═══════════════════════════════════════════════════════════════════════════════

class TestWalletRPCHttps:
    """MoneroWalletRPC should support HTTPS connections."""

    def test_default_is_http(self):
        """Default URL scheme should be http."""
        from sthrip.wallet import MoneroWalletRPC
        wallet = MoneroWalletRPC(host="localhost", port=18082)
        assert wallet.url.startswith("http://")

    def test_use_ssl_true_creates_https_url(self):
        """use_ssl=True should produce https:// URL."""
        from sthrip.wallet import MoneroWalletRPC
        wallet = MoneroWalletRPC(host="localhost", port=18082, use_ssl=True)
        assert wallet.url.startswith("https://")
        assert wallet.url == "https://localhost:18082/json_rpc"

    def test_use_ssl_false_creates_http_url(self):
        """use_ssl=False should produce http:// URL."""
        from sthrip.wallet import MoneroWalletRPC
        wallet = MoneroWalletRPC(host="localhost", port=18082, use_ssl=False)
        assert wallet.url == "http://localhost:18082/json_rpc"


# ═══════════════════════════════════════════════════════════════════════════════
# IMP-3: Throttled update_last_seen
# ═══════════════════════════════════════════════════════════════════════════════

class TestThrottledLastSeen:
    """update_last_seen should be throttled to avoid write amplification."""

    def test_update_last_seen_has_throttle(self):
        """Repository.update_last_seen should accept and respect throttle."""
        from sthrip.db.repository import AgentRepository
        from unittest.mock import MagicMock

        db = MagicMock()
        repo = AgentRepository(db)

        agent_id = uuid.uuid4()

        # First call should update
        repo.update_last_seen(agent_id)
        assert db.execute.called or db.query.called

    def test_update_last_seen_skips_within_throttle_window(self):
        """Second call within throttle window should be skipped."""
        from sthrip.db.repository import AgentRepository

        db = MagicMock()
        repo = AgentRepository(db)
        agent_id = uuid.uuid4()

        # First call
        repo.update_last_seen(agent_id)
        call_count_after_first = db.execute.call_count + db.query.call_count

        # Reset mock to check if second call does anything
        db.reset_mock()

        # Second call within window should be skipped
        repo.update_last_seen(agent_id)
        assert db.execute.call_count + db.query.call_count == 0, \
            "update_last_seen should skip DB write within throttle window"


# ═══════════════════════════════════════════════════════════════════════════════
# IMP-5: Concurrent webhook processing
# ═══════════════════════════════════════════════════════════════════════════════

class TestConcurrentWebhookProcessing:
    """Webhook events should be processed concurrently."""

    @pytest.mark.asyncio
    async def test_process_pending_events_is_concurrent(self):
        """process_pending_events should use asyncio concurrency."""
        from sthrip.services.webhook_service import WebhookService

        service = WebhookService()

        # Create mock events
        mock_events = []
        for i in range(5):
            evt = MagicMock()
            evt.id = uuid.uuid4()
            mock_events.append(evt)

        call_times = []

        async def slow_process_event(event_id):
            call_times.append(time.monotonic())
            await asyncio.sleep(0.05)  # 50ms each
            return MagicMock(success=True)

        with patch.object(service, "process_event", side_effect=slow_process_event):
            with patch("sthrip.services.webhook_service.get_db") as mock_db:
                mock_repo = MagicMock()
                mock_repo.get_pending_events.return_value = mock_events
                mock_db.return_value.__enter__ = MagicMock(return_value=MagicMock())
                mock_db.return_value.__exit__ = MagicMock(return_value=False)

                # Patch to return mock repo
                with patch("sthrip.services.webhook_service.WebhookRepository", return_value=mock_repo):
                    result = await service.process_pending_events(batch_size=100)

        # If concurrent: total time < 5*50ms = 250ms
        # If sequential: total time >= 250ms
        if len(call_times) >= 2:
            time_spread = call_times[-1] - call_times[0]
            assert time_spread < 0.15, \
                f"Events started {time_spread:.3f}s apart — should be concurrent (< 0.15s)"


# ═══════════════════════════════════════════════════════════════════════════════
# IMP-6: ApiSession removed, AuditLog.ip_address uses String
# ═══════════════════════════════════════════════════════════════════════════════

class TestModelCleanup:
    """ApiSession should be removed; AuditLog should use String for ip_address."""

    def test_api_session_model_removed(self):
        """ApiSession model should not exist in models module."""
        from sthrip.db import models
        assert not hasattr(models, "ApiSession"), \
            "ApiSession model should be removed (IMP-6)"

    def test_audit_log_ip_address_is_string(self):
        """AuditLog.ip_address should be String, not INET."""
        from sthrip.db.models import AuditLog
        col = AuditLog.__table__.columns["ip_address"]
        from sqlalchemy import String
        assert isinstance(col.type, String), \
            f"AuditLog.ip_address should be String, got {type(col.type)}"


# ═══════════════════════════════════════════════════════════════════════════════
# MIN-2: Local rate limiter check-before-increment
# ═══════════════════════════════════════════════════════════════════════════════

class TestRateLimiterCheckBeforeIncrement:
    """Local rate limiter should reject BEFORE incrementing at the limit."""

    def test_local_limiter_rejects_at_exact_limit(self):
        """Request at exactly the limit should be rejected, not processed."""
        from sthrip.services.rate_limiter import RateLimiter, RateLimitExceeded, RateLimitConfig

        with patch("sthrip.services.rate_limiter.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                redis_url=None,
                rate_limit_fail_open=True,
            )
            with patch("sthrip.services.rate_limiter.REDIS_AVAILABLE", False):
                limiter = RateLimiter()

        # Set limit to 3
        config = RateLimitConfig(requests_per_minute=3, burst_size=3)
        key = "test:check_before_incr"

        # Make 3 successful requests
        for _ in range(3):
            limiter._check_local(key, config, 1)

        # 4th request should be rejected
        with pytest.raises(RateLimitExceeded):
            limiter._check_local(key, config, 1)


# ═══════════════════════════════════════════════════════════════════════════════
# MIN-3: Privacy redaction for all address types
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrivacyRedactionAllAddresses:
    """Privacy redaction should apply to all wallet addresses, not just xmr."""

    def test_high_privacy_redacts_all_addresses(self):
        """High privacy agents should have ALL addresses redacted."""
        from sthrip.services.agent_registry import AgentRegistry
        from sthrip.db.models import PrivacyLevel

        registry = AgentRegistry()

        agent = MagicMock()
        agent.id = uuid.uuid4()
        agent.agent_name = "private-agent"
        agent.did = None
        agent.tier = MagicMock(value="free")
        agent.privacy_level = PrivacyLevel.HIGH
        agent.xmr_address = "4AAAA..."
        agent.base_address = "0xBBBB..."
        agent.solana_address = "SoLLL..."
        agent.reputation = None
        agent.verified_at = None
        agent.last_seen_at = None
        agent.created_at = datetime.now(timezone.utc)

        profile = registry._agent_to_profile(agent)

        assert profile.xmr_address is None, "xmr_address should be redacted"
        assert profile.base_address is None, "base_address should be redacted"
        assert profile.solana_address is None, "solana_address should be redacted"

    def test_paranoid_privacy_redacts_all_addresses(self):
        """Paranoid privacy agents should have ALL addresses redacted."""
        from sthrip.services.agent_registry import AgentRegistry
        from sthrip.db.models import PrivacyLevel

        registry = AgentRegistry()

        agent = MagicMock()
        agent.id = uuid.uuid4()
        agent.agent_name = "paranoid-agent"
        agent.did = None
        agent.tier = MagicMock(value="free")
        agent.privacy_level = PrivacyLevel.PARANOID
        agent.xmr_address = "4AAAA..."
        agent.base_address = "0xBBBB..."
        agent.solana_address = "SoLLL..."
        agent.reputation = None
        agent.verified_at = None
        agent.last_seen_at = None
        agent.created_at = datetime.now(timezone.utc)

        profile = registry._agent_to_profile(agent)

        assert profile.xmr_address is None
        assert profile.base_address is None
        assert profile.solana_address is None

    def test_medium_privacy_shows_all_addresses(self):
        """Medium privacy agents should have all addresses visible."""
        from sthrip.services.agent_registry import AgentRegistry
        from sthrip.db.models import PrivacyLevel

        registry = AgentRegistry()

        agent = MagicMock()
        agent.id = uuid.uuid4()
        agent.agent_name = "normal-agent"
        agent.did = None
        agent.tier = MagicMock(value="free")
        agent.privacy_level = PrivacyLevel.MEDIUM
        agent.xmr_address = "4AAAA..."
        agent.base_address = "0xBBBB..."
        agent.solana_address = "SoLLL..."
        agent.reputation = None
        agent.verified_at = None
        agent.last_seen_at = None
        agent.created_at = datetime.now(timezone.utc)

        profile = registry._agent_to_profile(agent)

        assert profile.xmr_address == "4AAAA..."
        assert profile.base_address == "0xBBBB..."
        assert profile.solana_address == "SoLLL..."


# ═══════════════════════════════════════════════════════════════════════════════
# IMP-4: pydantic-settings in requirements.lock
# ═══════════════════════════════════════════════════════════════════════════════

class TestRequirements:
    """pydantic-settings must be explicitly listed in requirements.lock."""

    def test_pydantic_settings_in_requirements_lock(self):
        from pathlib import Path
        req_file = Path(__file__).parent.parent / "requirements.lock"
        content = req_file.read_text()
        assert "pydantic-settings" in content, \
            "pydantic-settings must be listed explicitly in requirements.lock"


# ═══════════════════════════════════════════════════════════════════════════════
# IMP-8: Dockerfile CMD uses $PORT
# ═══════════════════════════════════════════════════════════════════════════════

class TestDockerfile:
    """Dockerfile CMD should use $PORT env var."""

    def test_dockerfile_uses_port_env_var(self):
        from pathlib import Path
        dockerfile = Path(__file__).parent.parent / "railway" / "Dockerfile.railway"
        content = dockerfile.read_text()
        assert "${PORT:-8000}" in content or "$PORT" in content, \
            "Dockerfile CMD should use $PORT environment variable"
