"""Tests for Cycle 4 production readiness fixes."""
import inspect
import json
import threading
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


# ── Task 20: Reject webhook URL credentials ────────────────────────────


class TestWebhookUrlCredentials:
    """Webhook URLs with embedded credentials must be rejected."""

    def test_rejects_url_with_username_password(self):
        from sthrip.services.url_validator import validate_url_target

        with pytest.raises(ValueError, match="[Cc]redential"):
            validate_url_target(
                "https://user:secret@example.com/hook",
                enforce_https=True,
            )

    def test_rejects_url_with_username_only(self):
        from sthrip.services.url_validator import validate_url_target

        with pytest.raises(ValueError, match="[Cc]redential"):
            validate_url_target(
                "https://apikey@example.com/hook",
                enforce_https=True,
            )

    def test_accepts_url_without_credentials(self):
        from sthrip.services.url_validator import validate_url_target

        # Should not raise (may raise SSRFBlockedError for unresolvable host,
        # but NOT ValueError about credentials)
        try:
            validate_url_target("https://example.com/hook", enforce_https=True)
        except ValueError as e:
            assert "credential" not in str(e).lower()
        except Exception:
            pass  # SSRF or DNS errors are fine


# ── Task 21: Decimal precision in fee_collector ────────────────────────


class TestFeeCollectorDecimalPrecision:
    """Fee amounts must be serialized as str, not float."""

    def test_get_pending_fees_returns_str_amounts(self):
        source = inspect.getsource(
            __import__("sthrip.services.fee_collector", fromlist=["FeeCollector"]).FeeCollector.get_pending_fees
        )
        assert "float(fee.amount)" not in source, (
            "get_pending_fees must use str(fee.amount), not float(fee.amount)"
        )

    def test_withdraw_fees_returns_str_total(self):
        source = inspect.getsource(
            __import__("sthrip.services.fee_collector", fromlist=["FeeCollector"]).FeeCollector.withdraw_fees
        )
        assert "float(total)" not in source, (
            "withdraw_fees must use str(total), not float(total)"
        )


# ── Task 22: RateLimiter Redis failure fallback ────────────────────────


class TestRateLimiterRedisFallback:
    """RateLimiter must fall back to local when Redis fails mid-request."""

    def test_check_redis_catches_connection_error(self):
        source = inspect.getsource(
            __import__("sthrip.services.rate_limiter", fromlist=["RateLimiter"]).RateLimiter._check_redis
        )
        # Must have exception handling for Redis errors
        assert "except" in source, (
            "_check_redis must handle Redis connection errors"
        )
        assert "ConnectionError" in source or "RedisError" in source or "Exception" in source, (
            "_check_redis must catch Redis connection errors"
        )


# ── Task 23: Admin enum filter validation ──────────────────────────────


class TestAdminEnumFilterValidation:
    """Admin views must validate enum filter values before querying."""

    def test_agents_list_validates_tier_param(self):
        from api.admin_ui import views
        source = inspect.getsource(views.agents_list)
        # Must validate tier before passing to query
        assert "AgentTier" in source or "pattern" in source or "tier in" in source or "valid_tier" in source, (
            "agents_list must validate tier parameter against allowed values"
        )

    def test_transactions_list_validates_status_param(self):
        from api.admin_ui import views
        source = inspect.getsource(views.transactions_list)
        assert "HubRouteStatus" in source or "pattern" in source or "status in" in source or "valid_status" in source, (
            "transactions_list must validate status parameter against allowed values"
        )


# ── Task 24: Webhook error sanitization ────────────────────────────────


class TestWebhookErrorSanitization:
    """Webhook error messages must not leak internal IPs."""

    def test_client_error_does_not_contain_raw_exception(self):
        source = inspect.getsource(
            __import__("sthrip.services.webhook_service", fromlist=["WebhookService"]).WebhookService._send_webhook
        )
        # Must NOT include raw str(e) in the error result
        assert 'f"Client error: {str(e)}"' not in source, (
            "_send_webhook must not pass raw aiohttp exception to error field "
            "(leaks resolved IPs)"
        )

    def test_unexpected_error_does_not_contain_raw_exception(self):
        source = inspect.getsource(
            __import__("sthrip.services.webhook_service", fromlist=["WebhookService"]).WebhookService._send_webhook
        )
        assert 'f"Unexpected error: {str(e)}"' not in source, (
            "_send_webhook must not pass raw exception message to error field"
        )


# ── Task 25: Monitoring dispatch thread safety ─────────────────────────


class TestMonitoringDispatchThreadSafety:
    """Alert dispatch must use a lock for shared mutable state."""

    def test_dispatch_module_has_lock(self):
        import sthrip.services.monitoring as mod
        assert hasattr(mod, "_dispatch_lock"), (
            "monitoring module must define _dispatch_lock for thread-safe alert dispatch"
        )

    def test_dispatch_uses_lock(self):
        import sthrip.services.monitoring as mod
        source = inspect.getsource(mod.dispatch_alert_webhook)
        assert "_dispatch_lock" in source, (
            "dispatch_alert_webhook must use _dispatch_lock"
        )


# ── Task 26: Idempotency sentinel cleanup on failure ──────────────────


class TestIdempotencySentinelCleanup:
    """store_response failure must log at CRITICAL level.

    The sentinel key is NOT released on Redis write failure because:
    1. The sentinel has a TTL (24h) and will expire naturally.
    2. Releasing the sentinel after a partial write could allow duplicate
       processing of the same idempotency key.
    3. A CRITICAL log alerts operators to investigate.
    """

    def test_store_response_logs_critical_on_redis_failure(self):
        source = inspect.getsource(
            __import__("sthrip.services.idempotency", fromlist=["IdempotencyStore"]).IdempotencyStore.store_response
        )
        # On Redis write failure, must log at CRITICAL level (not silently release)
        assert "critical" in source.lower(), (
            "store_response must log at CRITICAL level on Redis write failure "
            "to alert operators about stranded sentinel keys"
        )
