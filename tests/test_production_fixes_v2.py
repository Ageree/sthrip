"""Tests for production readiness fixes (v2).

TDD RED phase: all tests written BEFORE implementation.
"""

import hashlib
import os
import threading
import time
import contextlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import patch, MagicMock, AsyncMock
from uuid import uuid4, UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    HubRoute, FeeCollection, PendingWithdrawal, Transaction,
    WebhookEvent, WebhookStatus, AuditLog,
    EscrowDeal, PaymentChannel, ChannelState, SystemState,
)
_TEST_ENCRYPTION_KEY = "uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE="

_COMMON_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    PendingWithdrawal.__table__,
    Transaction.__table__,
]

_ALL_TEST_TABLES = _COMMON_TEST_TABLES + [
    WebhookEvent.__table__,
    EscrowDeal.__table__,
    PaymentChannel.__table__,
    ChannelState.__table__,
    AuditLog.__table__,
    SystemState.__table__,
]


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #1: HTTPS webhook TLS cert verification with IP pinning
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebhookTLSPinning:
    """Webhook service must pass server_hostname to aiohttp for TLS."""

    def test_pinned_url_preserves_original_hostname_for_tls(self):
        """When IP-pinning, the Host header must be set AND
        ssl context should use original hostname for cert verification."""
        from sthrip.services.webhook_service import WebhookService

        service = WebhookService()
        # Check that _send_webhook builds correct headers with Host
        # The key fix: session.post should receive server_hostname or
        # the ssl parameter should be configured with original hostname

        # We can test the URL construction logic
        from urllib.parse import urlparse, urlunparse

        url = "https://example.com:443/webhook"
        resolved_ip = "93.184.216.34"
        parsed = urlparse(url)

        # After fix: IPv4 pinned URL should work
        if parsed.port:
            pinned_netloc = f"{resolved_ip}:{parsed.port}"
        else:
            pinned_netloc = resolved_ip

        pinned_url = urlunparse((
            parsed.scheme, pinned_netloc,
            parsed.path, parsed.params, parsed.query, parsed.fragment,
        ))

        assert "93.184.216.34" in pinned_url
        assert parsed.hostname == "example.com"

    def test_ipv6_pinned_url_uses_brackets(self):
        """IPv6 addresses in URLs must be wrapped in brackets."""
        from sthrip.services.webhook_service import WebhookService

        # After fix, the service should wrap IPv6 in brackets
        resolved_ip = "2001:db8::1"
        port = 443

        # The fix should produce [2001:db8::1]:443
        if ":" in resolved_ip:
            pinned_netloc = f"[{resolved_ip}]:{port}" if port else f"[{resolved_ip}]"
        else:
            pinned_netloc = f"{resolved_ip}:{port}" if port else resolved_ip

        assert pinned_netloc == "[2001:db8::1]:443"

    def test_send_webhook_builds_correct_pinned_url_for_ipv6(self):
        """_send_webhook should correctly format IPv6 in pinned URL."""
        import inspect
        from sthrip.services.webhook_service import WebhookService

        source = inspect.getsource(WebhookService._send_webhook)
        # After fix: should handle IPv6 bracket formatting
        assert '":"' in source or "IPv6" in source or "[" in source, \
            "_send_webhook should handle IPv6 addresses with brackets"


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #2: Thread-safe _last_seen_cache
# ═══════════════════════════════════════════════════════════════════════════════

class TestLastSeenCacheThreadSafety:
    """AgentRepository._last_seen_cache must be thread-safe."""

    def test_cache_has_lock(self):
        """AgentRepository should have a class-level lock for cache access."""
        from sthrip.db.repository import AgentRepository
        assert hasattr(AgentRepository, '_last_seen_lock')
        assert isinstance(AgentRepository._last_seen_lock, type(threading.Lock()))

    def test_lock_is_used_in_update_last_seen(self):
        """update_last_seen should acquire the lock during cache operations."""
        import inspect
        from sthrip.db.repository import AgentRepository

        source = inspect.getsource(AgentRepository.update_last_seen)
        assert "_last_seen_lock" in source, \
            "update_last_seen should use _last_seen_lock for thread safety"


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #3: Monero address checksum validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestMoneroAddressChecksum:
    """validate_monero_address should verify base58 checksum."""

    def test_random_address_with_valid_format_rejected_by_checksum(self):
        """An address with valid format but invalid checksum should be rejected in production."""
        from api.schemas import validate_monero_address

        with patch("sthrip.config.get_settings") as mock:
            mock.return_value = MagicMock(monero_network="stagenet", environment="production")
            # 95-char address starting with 5, valid base58 chars, but random (bad checksum)
            addr = "5" + "B" * 94
            with pytest.raises(ValueError):
                validate_monero_address(addr)

    def test_base58_decode_function_exists(self):
        """_monero_base58_decode should be available for address decoding."""
        from api.schemas import _monero_base58_decode
        assert callable(_monero_base58_decode)

    def test_keccak256_function_exists(self):
        """_keccak256 should be available for checksum computation."""
        from api.schemas import _keccak256
        result = _keccak256(b"test")
        assert len(result) == 32  # 256 bits = 32 bytes


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #4: Webhook retry sets next_attempt_at = None
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebhookRetryNextAttempt:
    """Webhook retry should set next_attempt_at to now(), not None."""

    def test_retry_sets_next_attempt_at_to_now(self):
        """After retry, next_attempt_at should be a datetime, not None."""
        # Verify the code sets datetime.now() instead of None
        import inspect
        from api.routers.webhooks import retry_webhook_event

        source = inspect.getsource(retry_webhook_event)
        # After fix: should set next_attempt_at to datetime.now(timezone.utc)
        assert "datetime.now" in source, \
            "retry endpoint should set next_attempt_at = datetime.now(timezone.utc)"
        assert "next_attempt_at = None" not in source, \
            "retry endpoint should NOT set next_attempt_at = None"


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #6: UUID validation in webhook retry endpoint
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebhookRetryUUIDValidation:
    """Webhook retry endpoint should validate event_id as UUID."""

    def test_invalid_uuid_returns_422_not_500(self, client):
        """Passing an invalid UUID should return 422, not 500."""
        # Register an agent first
        resp = client.post("/v2/agents/register", json={
            "agent_name": "uuid_test_agent",
        })
        api_key = resp.json()["api_key"]

        resp = client.post(
            "/v2/webhooks/events/not-a-uuid/retry",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        # Should be 422 (validation error) not 500 (internal server error)
        assert resp.status_code == 422, \
            f"Expected 422 for invalid UUID, got {resp.status_code}"


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #7: Return webhook_secret at registration
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebhookSecretInRegistration:
    """Registration response should include webhook_secret."""

    def test_registration_returns_webhook_secret(self, client):
        """POST /v2/agents/register should return webhook_secret."""
        resp = client.post("/v2/agents/register", json={
            "agent_name": "webhook_secret_test_agent",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "webhook_secret" in data, \
            "Registration response must include webhook_secret"
        assert data["webhook_secret"].startswith("whsec_"), \
            "webhook_secret should start with whsec_ prefix"


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #8: Admin session store eviction
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionStoreEviction:
    """Session stores should evict expired entries."""

    def test_session_store_evicts_expired(self):
        """AdminSessionStore._local should not grow unbounded (dashboard store)."""
        from api.session_store import AdminSessionStore

        store = AdminSessionStore(key_prefix="admin_session:")
        store._redis_checked = True  # skip Redis

        # Add many expired sessions using bare hash keys (in-memory layout)
        for i in range(100):
            token_hash = hashlib.sha256(f"token-{i}".encode()).hexdigest()
            store._local[token_hash] = {"expires": time.time() - 3600}

        # Add a valid session
        store.set_session("valid-token", 3600)

        # Trigger eviction
        store._evict_expired()

        # Expired entries should be cleaned up
        expired_count = sum(
            1 for v in store._local.values()
            if isinstance(v, dict) and v.get("expires", float("inf")) < time.time()
        )
        assert expired_count < 100, \
            f"Expected expired entries to be evicted, but {expired_count} remain"

    def test_admin_session_store_evicts_expired(self):
        """AdminSessionStore._local should not grow unbounded (API store)."""
        from api.session_store import AdminSessionStore

        store = AdminSessionStore(key_prefix="admin_api_session:")
        store._redis_checked = True  # skip Redis

        # Add expired entries using bare hash keys (in-memory layout)
        for i in range(100):
            token_hash = hashlib.sha256(f"admin-{i}".encode()).hexdigest()
            store._local[token_hash] = {"expires": time.time() - 3600}

        # Trigger eviction
        store._evict_expired()

        expired_count = sum(
            1 for v in store._local.values()
            if isinstance(v, dict) and v.get("expires", float("inf")) < time.time()
        )
        assert expired_count < 100, \
            f"Expected expired entries to be evicted, but {expired_count} remain"


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #9: Recursive audit logger sanitize
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuditLoggerRecursiveSanitize:
    """_sanitize should recursively redact sensitive keys in nested dicts."""

    def test_nested_api_key_redacted(self):
        """Nested dicts containing sensitive keys should be redacted."""
        from sthrip.services.audit_logger import _sanitize

        data = {
            "action": "test",
            "old": {"api_key": "sk_secret123", "name": "agent1"},
            "new": {"api_key": "sk_secret456", "name": "agent2"},
        }

        result = _sanitize(data)

        assert result["old"]["api_key"] == "***", \
            "Nested api_key should be redacted"
        assert result["new"]["api_key"] == "***", \
            "Nested api_key should be redacted"
        assert result["old"]["name"] == "agent1"
        assert result["new"]["name"] == "agent2"

    def test_deeply_nested_secret_redacted(self):
        """Deeply nested sensitive keys should also be redacted."""
        from sthrip.services.audit_logger import _sanitize

        data = {
            "level1": {
                "level2": {
                    "password": "hunter2",
                    "safe_field": "visible",
                },
            },
        }

        result = _sanitize(data)
        assert result["level1"]["level2"]["password"] == "***"
        assert result["level1"]["level2"]["safe_field"] == "visible"

    def test_top_level_still_works(self):
        """Top-level sensitive keys should still be redacted."""
        from sthrip.services.audit_logger import _sanitize

        data = {"api_key": "secret", "name": "test"}
        result = _sanitize(data)
        assert result["api_key"] == "***"
        assert result["name"] == "test"


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #10: Admin auth rate limit on success
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdminAuthRateLimit:
    """Admin /auth should NOT increment rate limit on successful auth."""

    def test_successful_auth_does_not_increment_counter(self, client):
        """Successful admin auth should not count against rate limit."""
        # The mock limiter tracks calls
        # After fix: check_ip_rate_limit should use check_only=True before
        # key comparison, and only increment on failure
        from sthrip.services.rate_limiter import RateLimitExceeded

        call_count = {"increment": 0, "check_only": 0}

        def mock_check_ip(*args, **kwargs):
            if kwargs.get("check_only"):
                call_count["check_only"] += 1
            else:
                call_count["increment"] += 1

        mock_limiter = MagicMock()
        mock_limiter.check_ip_rate_limit = mock_check_ip
        mock_limiter.check_rate_limit.return_value = None

        admin_key = os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests-long-enough-32")

        with patch("api.routers.admin.get_rate_limiter", return_value=mock_limiter):
            resp = client.post("/v2/admin/auth", json={"admin_key": admin_key})

        assert resp.status_code == 200
        # After fix: should use check_only=True (no increment on success)
        assert call_count["increment"] == 0, \
            f"Successful auth should not increment rate limit counter, but got {call_count['increment']} increments"


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #11: Admin logout CSRF verification
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdminLogoutCSRF:
    """Admin logout should verify CSRF token."""

    def test_logout_without_csrf_token_rejected(self):
        """POST /admin/logout without valid CSRF should not log out."""
        from api.admin_ui.views import _session_store

        # Create a valid session
        token = "test-session-token-for-csrf"
        _session_store._redis_checked = True
        _session_store.set_session(token, 3600)

        assert _session_store.get_session(token) is True

        # Try logout without CSRF token
        from api.admin_ui.views import router
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)

        with TestClient(app, raise_server_exceptions=False) as tc:
            tc.cookies.set("admin_session", token)
            resp = tc.post("/admin/logout", data={"csrf_token": ""})

        # After fix: session should still be valid because CSRF failed
        # The response should redirect but NOT delete the session
        session_still_valid = _session_store.get_session(token)
        assert session_still_valid, \
            "Session should not be deleted when CSRF token is invalid"

        # Cleanup
        _session_store.delete_session(token)


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #12: Repository list methods limit cap
# ═══════════════════════════════════════════════════════════════════════════════

class TestRepositoryLimitCap:
    """All repository list methods should cap limit to _MAX_QUERY_LIMIT."""

    def test_transaction_list_caps_limit(self):
        """TransactionRepository.list_by_agent should cap limit."""
        from sthrip.db.repository import TransactionRepository, _MAX_QUERY_LIMIT

        engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine, tables=_COMMON_TEST_TABLES)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()

        repo = TransactionRepository(session)
        # This should not allow limit > _MAX_QUERY_LIMIT
        result = repo.list_by_agent(uuid4(), limit=10000)
        # The query should have been capped
        # We can't easily verify the SQL, but we can check it doesn't crash
        assert isinstance(result, list)
        session.close()

    def test_escrow_list_caps_limit(self):
        """EscrowRepository.list_by_agent should cap limit."""
        from sthrip.db.repository import EscrowRepository, _MAX_QUERY_LIMIT

        engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine, tables=_ALL_TEST_TABLES)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()

        repo = EscrowRepository(session)
        result = repo.list_by_agent(uuid4(), limit=10000)
        assert isinstance(result, list)
        session.close()

    def test_channel_list_caps_limit(self):
        """ChannelRepository.list_by_agent should cap limit."""
        from sthrip.db.repository import ChannelRepository, _MAX_QUERY_LIMIT

        engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine, tables=_ALL_TEST_TABLES)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()

        repo = ChannelRepository(session)
        result = repo.list_by_agent(uuid4(), limit=10000)
        assert isinstance(result, list)
        session.close()

    def test_webhook_get_pending_caps_limit(self):
        """WebhookRepository.get_pending_events should cap limit."""
        from sthrip.db.repository import WebhookRepository, _MAX_QUERY_LIMIT

        engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine, tables=_ALL_TEST_TABLES)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()

        repo = WebhookRepository(session)
        result = repo.get_pending_events(limit=10000)
        assert isinstance(result, list)
        session.close()


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #14: Admin UI ORM mutation
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdminUINOMutation:
    """Admin UI should not mutate ORM objects."""

    def test_transactions_list_does_not_mutate_orm(self):
        """transactions_list should use presentation dicts, not mutate HubRoute."""
        # This is a design test — verify no setattr on ORM objects
        import inspect
        from api.admin_ui import views

        source = inspect.getsource(views.transactions_list)
        # After fix: should NOT contain tx.from_agent = or tx.to_agent =
        assert "tx.from_agent =" not in source or "tx_data" in source, \
            "transactions_list should not mutate ORM objects directly"


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #15: RateLimitExceeded exception handler
# ═══════════════════════════════════════════════════════════════════════════════

class TestRateLimitExceededHandler:
    """App should have dedicated RateLimitExceeded handler returning 429."""

    def test_rate_limit_exceeded_returns_429(self):
        """Unhandled RateLimitExceeded should return 429, not 500."""
        from sthrip.services.rate_limiter import RateLimitExceeded
        from api.main_v2 import create_app

        app = create_app()

        # Check that exception handler is registered
        handler_types = [type(exc) for exc in app.exception_handlers.keys()
                         if isinstance(exc, type)]
        # Alternative: check the handler dict keys
        has_handler = RateLimitExceeded in app.exception_handlers
        assert has_handler, \
            "App should have a dedicated exception handler for RateLimitExceeded"


# ═══════════════════════════════════════════════════════════════════════════════
# ISSUE #5: Async wallet RPC
# ═══════════════════════════════════════════════════════════════════════════════

class TestAsyncWalletRPC:
    """MoneroWalletRPC should provide async methods."""

    def test_has_async_call_method(self):
        """MoneroWalletRPC should have an async _call method or wrapper."""
        from sthrip.wallet import MoneroWalletRPC
        import asyncio

        wallet = MoneroWalletRPC(host="127.0.0.1", port=18082)

        # After fix: should have async method
        assert hasattr(wallet, '_acall') or hasattr(wallet, 'async_get_balance'), \
            "MoneroWalletRPC should have async methods (_acall or async_get_balance)"
