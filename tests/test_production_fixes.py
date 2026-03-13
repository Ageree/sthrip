"""
TDD tests for production readiness fixes.

These tests cover CRITICAL and HIGH issues found during code review:
1. redis_url hardcoded default (CRITICAL)
2. admin_key not in Sentry scrub pattern (CRITICAL)
3. Idempotency key not released on withdrawal failure (CRITICAL)
4. tier query param unvalidated (HIGH)
5. payment_id accepts arbitrary string (HIGH)
6. last_error exposes internal infrastructure details (HIGH)
7. get_current_agent bypasses app.state for rate_limiter (HIGH)
8. privacy_level not coerced to enum (HIGH)
"""

import re
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from uuid import uuid4


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Redis URL default must be empty string
# ═══════════════════════════════════════════════════════════════════════════════


class TestRedisUrlDefault:
    """redis_url must default to empty string so _validate_settings warns."""

    def test_redis_url_default_is_empty(self):
        """Settings.redis_url default must be empty so missing-Redis check fires."""
        from sthrip.config import Settings

        # Check the field default directly from the model schema
        field_info = Settings.model_fields["redis_url"]
        assert field_info.default == "", (
            f"redis_url default must be '' to trigger the missing-Redis warning, "
            f"got '{field_info.default}'"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Sentry scrub must cover admin_key and webhook_encryption_key
# ═══════════════════════════════════════════════════════════════════════════════


class TestSentryScrubPattern:
    """_SENSITIVE_KEY_RE must match admin_key and webhook_encryption_key."""

    def _get_key_pattern(self):
        """Extract the _SENSITIVE_KEY_RE pattern from _init_sentry."""
        # We test the pattern directly since _init_sentry is not easily unit-testable
        import importlib
        import ast

        with open("api/main_v2.py") as f:
            source = f.read()

        # Find the _SENSITIVE_KEY_RE pattern string
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "_SENSITIVE_KEY_RE":
                        # Extract the pattern from re.compile call
                        if isinstance(node.value, ast.Call):
                            pattern_arg = node.value.args[0]
                            if isinstance(pattern_arg, ast.Constant):
                                return re.compile(pattern_arg.value, re.IGNORECASE)
        # Fallback: just import and check
        pytest.skip("Could not extract _SENSITIVE_KEY_RE from source")

    def test_scrub_catches_admin_key(self):
        """Sentry scrubber must match 'admin_key' dict keys."""
        pattern = self._get_key_pattern()
        assert pattern.search("admin_key"), (
            "_SENSITIVE_KEY_RE must match 'admin_key' to prevent Sentry leak"
        )

    def test_scrub_catches_webhook_encryption_key(self):
        """Sentry scrubber must match 'webhook_encryption_key' dict keys."""
        pattern = self._get_key_pattern()
        assert pattern.search("webhook_encryption_key"), (
            "_SENSITIVE_KEY_RE must match 'webhook_encryption_key'"
        )

    def test_scrub_catches_hmac_secret(self):
        """Sentry scrubber must match 'api_key_hmac_secret' dict keys."""
        pattern = self._get_key_pattern()
        assert pattern.search("api_key_hmac_secret"), (
            "_SENSITIVE_KEY_RE must match 'api_key_hmac_secret'"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Idempotency key must be released on withdrawal failure after deduction
# ═══════════════════════════════════════════════════════════════════════════════


class TestIdempotencyKeyRelease:
    """Idempotency key must be released when withdrawal fails, even after balance deduction."""

    def test_idempotency_released_when_pending_id_set(self):
        """When pending_id is set and RPC fails, idempotency key must still release."""
        with open("api/routers/balance.py") as f:
            source = f.read()

        # The withdraw_balance except block must not condition release on
        # pending_id is None — the key must always be released on error
        # to allow retry.
        assert "pending_id is None" not in source, (
            "Idempotency key release must not be conditioned on pending_id is None. "
            "The key must be released on any exception to allow the agent to retry."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Tier query param must be validated
# ═══════════════════════════════════════════════════════════════════════════════


class TestTierValidation:
    """GET /v2/agents tier param must be validated against AgentTier enum values."""

    def test_discover_agents_tier_has_pattern_validation(self):
        """tier parameter on discover_agents must have pattern validation."""
        import inspect
        from api.routers.agents import discover_agents

        sig = inspect.signature(discover_agents)
        tier_param = sig.parameters.get("tier")
        assert tier_param is not None, "discover_agents must have a tier parameter"

        # Check that the Query has a pattern constraint
        default = tier_param.default
        # FastAPI/Pydantic stores pattern in metadata list
        has_pattern = False
        if hasattr(default, "metadata"):
            for m in default.metadata:
                if hasattr(m, "pattern") and m.pattern:
                    has_pattern = True
                    break
        # Also check direct attribute (older FastAPI)
        if not has_pattern:
            has_pattern = getattr(default, "pattern", None) is not None
        assert has_pattern, (
            "tier parameter must have pattern validation via Query(pattern=...)"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 5. payment_id must be UUID type
# ═══════════════════════════════════════════════════════════════════════════════


class TestPaymentIdValidation:
    """GET /v2/payments/{payment_id} must validate payment_id as UUID."""

    def test_get_payment_uses_uuid_type(self):
        """payment_id parameter must be typed as UUID, not str."""
        import inspect
        from api.routers.payments import get_payment

        sig = inspect.signature(get_payment)
        payment_id_param = sig.parameters.get("payment_id")
        assert payment_id_param is not None

        annotation = payment_id_param.annotation
        # Accept UUID or str-with-pattern
        from uuid import UUID
        assert annotation is UUID or (hasattr(annotation, "__origin__") and annotation.__origin__ is UUID), (
            f"payment_id must be typed as UUID, got {annotation}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 6. last_error must be sanitized
# ═══════════════════════════════════════════════════════════════════════════════


class TestLastErrorSanitization:
    """Webhook event last_error must not expose internal infrastructure details."""

    def test_last_error_is_sanitized(self):
        """last_error containing internal details must be redacted."""
        with open("api/routers/webhooks.py") as f:
            source = f.read()

        # The raw `e.last_error` must not be returned directly
        # Should use a sanitization function or generic message
        assert '"last_error": e.last_error' not in source, (
            "last_error must be sanitized before returning to agents"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 7. get_current_agent must use app.state.rate_limiter
# ═══════════════════════════════════════════════════════════════════════════════


class TestRateLimiterDI:
    """get_current_agent must use request.app.state for rate_limiter."""

    def test_get_current_agent_uses_app_state(self):
        """get_current_agent must prefer request.app.state.rate_limiter."""
        with open("api/deps.py") as f:
            source = f.read()

        # Find the get_current_agent function body
        func_start = source.index("async def get_current_agent")
        next_func = source.find("\ndef ", func_start + 1)
        if next_func == -1:
            next_func = source.find("\nasync def ", func_start + 1)
        if next_func == -1:
            next_func = len(source)
        func_body = source[func_start:next_func]

        assert "app.state.rate_limiter" in func_body, (
            "get_current_agent must use request.app.state.rate_limiter "
            "as primary source (with fallback to get_rate_limiter())"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 8. privacy_level must be coerced to PrivacyLevel enum
# ═══════════════════════════════════════════════════════════════════════════════


class TestPrivacyLevelCoercion:
    """update_agent_settings must coerce privacy_level to PrivacyLevel enum."""

    def test_privacy_level_coerced_to_enum(self):
        """setattr for privacy_level must use PrivacyLevel(value), not raw string."""
        with open("api/routers/agents.py") as f:
            source = f.read()

        # The update_agent_settings function must coerce privacy_level to enum
        func_start = source.index("async def update_agent_settings")
        next_func_idx = source.find("\n@router.", func_start + 1)
        if next_func_idx == -1:
            next_func_idx = len(source)
        func_body = source[func_start:next_func_idx]

        # Must explicitly reference PrivacyLevel for enum coercion
        assert "PrivacyLevel(" in func_body, (
            "update_agent_settings must coerce privacy_level string to "
            "PrivacyLevel enum via PrivacyLevel(value) before setattr"
        )

    def test_privacy_level_enum_values_match(self):
        """PrivacyLevel enum values must match the allowed pattern."""
        from sthrip.db.enums import PrivacyLevel
        expected = {"low", "medium", "high", "paranoid"}
        actual = {pl.value for pl in PrivacyLevel}
        assert actual == expected
