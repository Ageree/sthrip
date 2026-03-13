"""Tests for Cycle 6 production readiness fixes."""
import inspect
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


# -- Task 36: PaymentType enum must include WITHDRAWAL and DEPOSIT -----------

class TestPaymentTypeEnum:
    """PaymentType enum must have WITHDRAWAL and DEPOSIT values."""

    def test_withdrawal_value_exists(self):
        from sthrip.db.enums import PaymentType
        assert hasattr(PaymentType, "WITHDRAWAL"), (
            "PaymentType must have a WITHDRAWAL member"
        )
        assert PaymentType.WITHDRAWAL.value == "withdrawal"

    def test_deposit_value_exists(self):
        from sthrip.db.enums import PaymentType
        assert hasattr(PaymentType, "DEPOSIT"), (
            "PaymentType must have a DEPOSIT member"
        )
        assert PaymentType.DEPOSIT.value == "deposit"

    def test_onchain_withdrawal_uses_withdrawal_enum(self):
        """_process_onchain_withdrawal must use the enum, not a raw string."""
        source = inspect.getsource(
            __import__("api.routers.balance", fromlist=["_process_onchain_withdrawal"])._process_onchain_withdrawal
        )
        assert 'payment_type="withdrawal"' in source or "PaymentType.WITHDRAWAL" in source, (
            "_process_onchain_withdrawal must use payment_type='withdrawal'"
        )

    def test_deposit_monitor_uses_deposit_type(self):
        """Deposit monitor must record incoming transfers as 'deposit', not 'hub_routing'."""
        from sthrip.services import deposit_monitor
        source = inspect.getsource(deposit_monitor.DepositMonitor._handle_new_transfer)
        assert "deposit" in source and "hub_routing" not in source.split("payment_type")[1][:30], (
            "_handle_new_transfer must use payment_type='deposit'"
        )


# -- Task 37: Self-send check must apply in ledger mode too -----------------

class TestSelfSendLedgerMode:
    """Withdrawal self-send check must not be skipped in ledger mode."""

    def test_check_self_send_always_true(self):
        """_deduct_and_create_pending must always receive check_self_send=True."""
        source = inspect.getsource(
            __import__("api.routers.balance", fromlist=["withdraw_balance"]).withdraw_balance
        )
        # Must NOT have check_self_send=(hub_mode == "onchain")
        assert 'check_self_send=(hub_mode == "onchain")' not in source, (
            "Self-send check must not be conditional on hub_mode"
        )
        assert "check_self_send=True" in source or "check_self_send = True" in source, (
            "check_self_send must always be True"
        )


# -- Task 38: drop_tables() must only allow dev environment -----------------

class TestDropTablesRestriction:
    """drop_tables() must only be callable in dev environment."""

    def test_drop_tables_rejects_stagenet(self):
        source = inspect.getsource(
            __import__("sthrip.db.database", fromlist=["drop_tables"]).drop_tables
        )
        # Must NOT allow "test" environment (it's not a valid env anyway)
        assert '"test"' not in source, (
            "drop_tables must not allow 'test' environment — only 'dev'"
        )

    @patch("sthrip.db.database.get_settings")
    def test_drop_tables_raises_for_stagenet(self, mock_settings):
        mock_settings.return_value.environment = "stagenet"
        from sthrip.db.database import drop_tables
        with pytest.raises(RuntimeError, match="disabled"):
            drop_tables()

    @patch("sthrip.db.database.get_settings")
    def test_drop_tables_raises_for_staging(self, mock_settings):
        mock_settings.return_value.environment = "staging"
        from sthrip.db.database import drop_tables
        with pytest.raises(RuntimeError, match="disabled"):
            drop_tables()


# -- Task 39: Webhook catch-all must log the exception ----------------------

class TestWebhookExceptionLogging:
    """Webhook _send_webhook must log unexpected exceptions."""

    def test_catch_all_logs_exception(self):
        source = inspect.getsource(
            __import__("sthrip.services.webhook_service", fromlist=["WebhookService"]).WebhookService._send_webhook
        )
        # Find the catch-all except Exception block and verify it logs
        lines = source.split("\n")
        in_catch_all = False
        has_log = False
        for line in lines:
            stripped = line.strip()
            if "except Exception" in stripped and "ClientError" not in stripped:
                in_catch_all = True
            if in_catch_all and ("logger.exception" in stripped or "logger.error" in stripped):
                has_log = True
                break
            if in_catch_all and stripped.startswith("return"):
                break
        assert has_log, (
            "Webhook _send_webhook catch-all except Exception must log with "
            "logger.exception() or logger.error()"
        )


# -- Task 40: RPC error must not leak infra details -------------------------

class TestRpcErrorSanitization:
    """PendingWithdrawal reason must not contain raw exception messages."""

    def test_rpc_error_reason_sanitized(self):
        source = inspect.getsource(
            __import__("api.routers.balance", fromlist=["_process_onchain_withdrawal"])._process_onchain_withdrawal
        )
        # Must NOT interpolate raw {e} into reason
        assert "f\"RPC error, verify on-chain before refunding: {e}\"" not in source, (
            "PendingWithdrawal reason must not contain raw exception message"
        )
        # Should use type name only
        assert "type(e).__name__" in source or "__class__.__name__" in source, (
            "PendingWithdrawal reason should use exception type name, not full message"
        )


# -- Task 41: min_trust_score=0 must not be treated as falsy ----------------

class TestMinTrustScoreZero:
    """min_trust_score=0 must still apply the filter."""

    def test_discover_agents_uses_is_not_none(self):
        from sthrip.services import agent_registry
        source = inspect.getsource(agent_registry.AgentRegistry.discover_agents)
        assert "min_trust_score is not None" in source, (
            "discover_agents must use 'is not None' check, not truthiness"
        )

    def test_count_agents_uses_is_not_none(self):
        from sthrip.services import agent_registry
        source = inspect.getsource(agent_registry.AgentRegistry.count_agents)
        assert "min_trust_score is not None" in source, (
            "count_agents must use 'is not None' check, not truthiness"
        )


# -- Task 42: Webhook manual retry must reset attempt_count to 0 ------------

class TestWebhookRetryReset:
    """Manual retry must reset attempt_count to 0, not decrement by 1."""

    def test_retry_resets_attempt_count(self):
        source = inspect.getsource(
            __import__("api.routers.webhooks", fromlist=["retry_webhook_event"]).retry_webhook_event
        )
        # Must NOT decrement: max((event.attempt_count or 0) - 1, 0)
        assert "- 1" not in source, (
            "Manual retry must reset attempt_count to 0, not decrement by 1"
        )
        assert "attempt_count = 0" in source, (
            "Manual retry must set attempt_count = 0"
        )
