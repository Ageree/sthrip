"""Tests for Cycle 7 production readiness fixes."""
import inspect
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


# -- Task 43: CORS must include PATCH method ---------------------------------

class TestCorsPatchMethod:
    """CORS allow_methods must include PATCH for /v2/me/settings."""

    def test_cors_includes_patch(self):
        source = inspect.getsource(
            __import__("api.middleware", fromlist=["configure_middleware"]).configure_middleware
        )
        assert '"PATCH"' in source or "'PATCH'" in source, (
            "CORS allow_methods must include PATCH"
        )


# -- Task 44: Idempotency key must always be released on error ---------------
# Updated: The original test enforced the wrong behavior. Conditioning
# release on pending_id is None locked agents out of retrying failed
# withdrawals after balance deduction — a CRITICAL fund-lock bug.

class TestWithdrawalIdempotencyRelease:
    """Idempotency key must always be released on error to allow retry."""

    def test_always_release_idempotency_key_on_error(self):
        """The except block must always release the key, not condition on pending_id."""
        source = inspect.getsource(
            __import__("api.routers.balance", fromlist=["withdraw_balance"]).withdraw_balance
        )
        # Must NOT condition release on pending_id — always release
        assert "pending_id is None" not in source, (
            "Idempotency key must always be released on error to allow retry. "
            "The pending_id is None condition locks out retries after RPC failure."
        )


# -- Task 45: Hub payment must enforce minimum amount -----------------------

class TestHubPaymentMinimumAmount:
    """HubPaymentRequest.amount must have a minimum to prevent 100% fee rate."""

    def test_amount_has_minimum(self):
        from api.schemas import HubPaymentRequest
        import pydantic

        # Attempt to create a request with amount below min_fee (0.0001)
        with pytest.raises(pydantic.ValidationError):
            HubPaymentRequest(
                to_agent_name="test_agent",
                amount=Decimal("0.00001"),
            )

    def test_amount_at_minimum_succeeds(self):
        from api.schemas import HubPaymentRequest
        req = HubPaymentRequest(
            to_agent_name="test_agent",
            amount=Decimal("0.0001"),
        )
        assert req.amount == Decimal("0.0001")


# -- Task 46: withdraw_fees must lock rows before computing total -----------

class TestWithdrawFeesLocking:
    """withdraw_fees must use FOR UPDATE to prevent TOCTOU on fee sum."""

    def test_withdraw_fees_uses_for_update(self):
        source = inspect.getsource(
            __import__("sthrip.services.fee_collector", fromlist=["FeeCollector"]).FeeCollector.withdraw_fees
        )
        assert "with_for_update" in source or "for_update" in source, (
            "withdraw_fees must lock rows with FOR UPDATE before computing total"
        )


# -- Task 47: PendingWithdrawal mutations must verify rowcount --------------

class TestPendingWithdrawalRowcount:
    """mark_completed and mark_needs_review must verify affected row count."""

    def test_mark_completed_checks_rowcount(self):
        source = inspect.getsource(
            __import__("sthrip.db.pending_withdrawal_repo", fromlist=["PendingWithdrawalRepository"])
            .PendingWithdrawalRepository.mark_completed
        )
        assert "rowcount" in source or "rows_affected" in source, (
            "mark_completed must verify the update affected exactly 1 row"
        )

    def test_mark_needs_review_checks_rowcount(self):
        source = inspect.getsource(
            __import__("sthrip.db.pending_withdrawal_repo", fromlist=["PendingWithdrawalRepository"])
            .PendingWithdrawalRepository.mark_needs_review
        )
        assert "rowcount" in source or "rows_affected" in source, (
            "mark_needs_review must verify the update affected exactly 1 row"
        )
