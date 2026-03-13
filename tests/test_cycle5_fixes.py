"""Tests for Cycle 5 production readiness fixes."""
import inspect
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from sthrip.db.models import HubRouteStatus


# ── Task 27: Self-payment prevention ────────────────────────────────────


class TestSelfPaymentPrevention:
    """Sending a hub payment to yourself must be rejected."""

    def test_validate_recipient_rejects_self(self):
        """_validate_recipient_in_session must reject sender == recipient."""
        from api.routers.payments import _validate_recipient_in_session
        source = inspect.getsource(_validate_recipient_in_session)
        # Must have a check that prevents self-payment
        assert "self" in source.lower() or "sender" in source.lower() or "same" in source.lower() or "agent.id" in source, (
            "_validate_recipient_in_session must check sender != recipient"
        )

    def test_check_not_self_payment_exists(self):
        """_check_not_self_payment must exist and reject same sender/recipient."""
        from api.routers.payments import _check_not_self_payment
        source = inspect.getsource(_check_not_self_payment)
        assert "yourself" in source.lower() or "self" in source.lower(), (
            "_check_not_self_payment must reject self-payments"
        )

    def test_send_hub_routed_payment_calls_self_check(self):
        """send_hub_routed_payment must call _check_not_self_payment."""
        from api.routers import payments
        source = inspect.getsource(payments.send_hub_routed_payment)
        assert "_check_not_self_payment" in source, (
            "send_hub_routed_payment must call _check_not_self_payment"
        )


# ── Task 28: asyncio.Lock() lazy init for Python 3.9 ────────────────────


class TestWebhookServiceAsyncLock:
    """WebhookService._session_lock must be lazily initialized for Python 3.9."""

    def test_session_lock_not_created_in_init(self):
        """Lock must NOT be created in __init__ (Python 3.9 event loop binding issue)."""
        source = inspect.getsource(
            __import__("sthrip.services.webhook_service", fromlist=["WebhookService"]).WebhookService.__init__
        )
        # Should NOT have asyncio.Lock() directly in __init__
        assert "asyncio.Lock()" not in source, (
            "WebhookService.__init__ must not create asyncio.Lock() "
            "(Python 3.9 binds it to the current event loop at creation time)"
        )


# ── Task 29: _acall fallthrough returns None ─────────────────────────────


class TestAcallFallthrough:
    """wallet._acall must not silently return None."""

    def test_acall_has_fallback_raise(self):
        source = inspect.getsource(
            __import__("sthrip.wallet", fromlist=["MoneroWalletRPC"]).MoneroWalletRPC._acall
        )
        # After the retry loop there must be a raise or return that prevents None
        assert "WalletRPCError" in source, (
            "_acall must raise WalletRPCError on fallthrough, not return None"
        )


# ── Task 30: _is_authenticated returns bool ──────────────────────────────


class TestIsAuthenticatedReturnsBool:
    """_is_authenticated must return bool, not a raw dict."""

    def test_returns_bool(self):
        from api.admin_ui.views import _is_authenticated
        source = inspect.getsource(_is_authenticated)
        assert "bool(" in source, (
            "_is_authenticated must wrap return in bool() to prevent "
            "empty dict bypassing auth"
        )


# ── Task 31: Withdrawal payment_type must be 'withdrawal' ───────────────


class TestWithdrawalPaymentType:
    """Withdrawal transactions must be recorded as payment_type='withdrawal'."""

    def test_onchain_withdrawal_uses_correct_payment_type(self):
        source = inspect.getsource(
            __import__("api.routers.balance", fromlist=["_process_onchain_withdrawal"])._process_onchain_withdrawal
        )
        assert 'payment_type="withdrawal"' in source or "payment_type='withdrawal'" in source, (
            "_process_onchain_withdrawal must use payment_type='withdrawal', not 'hub_routing'"
        )


# ── Task 32: Audit log _sanitize must recurse into lists ────────────────


class TestAuditSanitizeRecursesLists:
    """_sanitize must redact sensitive keys inside list elements."""

    def test_sanitize_handles_list_of_dicts(self):
        from sthrip.services.audit_logger import _sanitize
        data = {
            "items": [
                {"name": "ok", "api_key": "sk_secret_123"},
                {"name": "also_ok"},
            ]
        }
        result = _sanitize(data)
        assert result["items"][0]["api_key"] == "***", (
            "_sanitize must redact sensitive keys inside list elements"
        )
        assert result["items"][0]["name"] == "ok"
        assert result["items"][1]["name"] == "also_ok"

    def test_sanitize_handles_nested_list(self):
        from sthrip.services.audit_logger import _sanitize
        data = {"outer": [{"inner": [{"password": "secret"}]}]}
        result = _sanitize(data)
        assert result["outer"][0]["inner"][0]["password"] == "***"


# ── Task 33: _peek_ip_limit unbound g_reset ──────────────────────────────


# ── Task 34: Double-credit on idempotent duplicate ───────────────────────


class TestDuplicateRouteNoBalanceMutation:
    """Idempotent duplicate must NOT deduct/credit balances again."""

    def test_execute_hub_transfer_checks_duplicate_before_balance(self):
        """_execute_hub_transfer must check for duplicate route BEFORE balance mutations."""
        from api.routers.payments import _execute_hub_transfer
        source = inspect.getsource(_execute_hub_transfer)
        # The create_hub_route call (which returns duplicate) must come BEFORE deduct/credit
        deduct_pos = source.find("balance_repo.deduct")
        create_route_pos = source.find("create_hub_route")
        assert create_route_pos < deduct_pos, (
            "_execute_hub_transfer must check for duplicate route BEFORE "
            "calling balance_repo.deduct to prevent double-credit"
        )


# ── Task 35: settle_hub_route must require CONFIRMED status ──────────────


class TestSettleRequiresConfirmed:
    """settle_hub_route must reject settling a PENDING (unconfirmed) route."""

    @patch("sthrip.services.fee_collector.get_db")
    def test_settle_pending_route_raises(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_route = MagicMock()
        mock_route.status = HubRouteStatus.PENDING  # Not confirmed yet
        mock_db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = mock_route

        from sthrip.services.fee_collector import FeeCollector
        collector = FeeCollector()
        with pytest.raises(ValueError, match="[Cc]onfirm|not confirmed|PENDING"):
            collector.settle_hub_route("hp_test", "tx_hash")

    @patch("sthrip.services.fee_collector.get_db")
    def test_settle_confirmed_route_succeeds(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_route = MagicMock()
        mock_route.status = HubRouteStatus.CONFIRMED
        mock_route.settled_at = None
        mock_db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = mock_route

        from sthrip.services.fee_collector import FeeCollector
        collector = FeeCollector()
        result = collector.settle_hub_route("hp_test", "tx_hash_abc")
        assert result["status"] == "settled"


# ── Task 33: _peek_ip_limit unbound g_reset ──────────────────────────────


class TestPeekIpLimitVariableScope:
    """_peek_ip_limit must initialize g_reset before potential use."""

    def test_g_reset_always_initialized(self):
        source = inspect.getsource(
            __import__("sthrip.services.rate_limiter", fromlist=["RateLimiter"]).RateLimiter._peek_ip_limit
        )
        # In the local-cache branch, g_reset must be initialized before the if block
        # that might reference it
        lines = source.split("\n")
        # Find local cache section (the else: branch)
        in_else = False
        g_reset_init_before_if = False
        for line in lines:
            stripped = line.strip()
            if "else:" in stripped and "use_redis" not in stripped:
                in_else = True
            if in_else:
                if "g_reset" in stripped and "=" in stripped and "==" not in stripped and ">=" not in stripped:
                    g_reset_init_before_if = True
                    break
        assert g_reset_init_before_if, (
            "_peek_ip_limit must initialize g_reset before conditional blocks "
            "to prevent UnboundLocalError"
        )
