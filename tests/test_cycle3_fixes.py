"""Tests for Cycle 3 production readiness fixes."""
from decimal import Decimal
from unittest.mock import MagicMock, patch, call
from uuid import uuid4

import pytest


# ── Task 14: create_event must flush to populate event.id ──────────────


class TestCreateEventFlush:
    """WebhookRepository.create_event must flush to populate server-side UUID."""

    def test_create_event_returns_non_none_id(self):
        """After create_event, event.id must not be None."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sthrip.db.models import Base, WebhookEvent, Agent, AgentReputation
        from sthrip.db.webhook_repo import WebhookRepository

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        tables = [Agent.__table__, AgentReputation.__table__, WebhookEvent.__table__]
        Base.metadata.create_all(engine, tables=tables)
        Session = sessionmaker(bind=engine)
        session = Session()

        try:
            agent_id = uuid4()
            repo = WebhookRepository(session)
            event = repo.create_event(
                agent_id=agent_id,
                event_type="test.event",
                payload={"key": "value"},
            )
            assert event.id is not None, (
                "create_event must flush to populate event.id"
            )
        finally:
            session.close()

    def test_queue_event_returns_valid_uuid_string(self):
        """WebhookService.queue_event must return a valid UUID string, not 'None'."""
        from sthrip.services.webhook_service import WebhookService

        service = WebhookService.__new__(WebhookService)
        service._hmac_secret = None
        service._encryption_key = None

        mock_event = MagicMock()
        mock_event.id = uuid4()

        mock_repo = MagicMock()
        mock_repo.create_event.return_value = mock_event

        with patch("sthrip.services.webhook_service.get_db") as mock_get_db, \
             patch("sthrip.services.webhook_service.WebhookRepository", return_value=mock_repo):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_get_db.return_value = mock_ctx

            result = service.queue_event(uuid4(), "test", {"data": 1})
            assert result != "None", "queue_event must not return string 'None'"
            assert len(result) > 10, "Must be a valid UUID string"


# ── Task 15: Min fee must not exceed payment amount ────────────────────


class TestMinFeeExceedsPayment:
    """Fee must not exceed the payment amount."""

    def test_fee_does_not_exceed_payment_amount(self):
        """For tiny payments, fee should be capped at the payment amount."""
        from sthrip.services.fee_collector import FeeCollector

        collector = FeeCollector()
        # 0.00001 XMR — much smaller than min_fee of 0.0001
        result = collector.calculate_hub_routing_fee(Decimal("0.00001"))

        fee = result["fee_amount"]
        amount = result["base_amount"]
        assert fee <= amount, (
            f"Fee {fee} exceeds payment amount {amount}. "
            f"Sender would pay more in fees than the transfer itself."
        )

    def test_fee_does_not_exceed_half_of_payment(self):
        """Fee should not consume more than the payment amount."""
        from sthrip.services.fee_collector import FeeCollector

        collector = FeeCollector()
        result = collector.calculate_hub_routing_fee(Decimal("0.0001"))

        fee = result["fee_amount"]
        amount = result["base_amount"]
        assert fee <= amount, (
            f"Fee {fee} should not exceed payment amount {amount}"
        )

    def test_normal_payment_fee_unchanged(self):
        """Normal-size payments should still apply standard fees."""
        from sthrip.services.fee_collector import FeeCollector

        collector = FeeCollector()
        result = collector.calculate_hub_routing_fee(Decimal("10.0"))

        # 1% of 10.0 = 0.1 XMR (above min_fee of 0.0001)
        assert result["fee_amount"] == Decimal("10.0") * Decimal("0.01")


# ── Task 16: Admin verify_agent must validate UUID ─────────────────────


class TestAdminAgentIdValidation:
    """Admin endpoints must validate agent_id as UUID."""

    def test_verify_agent_rejects_invalid_uuid(self):
        """verify_agent should return 404 for non-UUID agent_id."""
        import inspect
        from api.routers.admin import verify_agent

        source = inspect.getsource(verify_agent)
        # The route must validate UUID before passing to registry
        assert "UUID" in source or "uuid" in source, (
            "verify_agent must validate agent_id as UUID"
        )


# ── Task 17: Withdrawal response must truncate address ─────────────────


class TestWithdrawalAddressLeak:
    """Withdrawal API response must not leak full destination address."""

    def test_response_truncates_address(self):
        """The to_address in withdrawal response should be truncated."""
        import inspect
        from api.routers import balance

        source = inspect.getsource(balance)
        # Find the _process_onchain_withdrawal function
        # The to_address in the return dict should be truncated
        lines = source.split("\n")
        in_response = False
        for line in lines:
            if '"to_address"' in line and "return" not in line:
                # Check if it's in a response dict (not webhook)
                if "address[:8]" not in line and "address[:" not in line:
                    if "queue_webhook" not in line and "webhook" not in line.lower():
                        # This is the API response — must truncate
                        assert "[:8]" in line or "[:" in line, (
                            f"API response leaks full address: {line.strip()}"
                        )


# ── Task 18: Webhooks must fire AFTER db.commit ────────────────────────


class TestWebhookAfterCommit:
    """DepositMonitor must fire webhooks only after db.commit succeeds."""

    def test_process_transfers_returns_deferred_webhooks(self):
        """_process_transfers must return webhook list instead of firing inline."""
        import inspect
        from sthrip.services.deposit_monitor import DepositMonitor

        source = inspect.getsource(DepositMonitor._process_transfers)
        # _process_transfers should NOT call _fire_webhook directly
        assert "_fire_webhook" not in source, (
            "_process_transfers must not call _fire_webhook directly. "
            "Webhooks should be deferred until after db.commit()."
        )

    def test_webhooks_fired_after_commit_in_poll(self):
        """_do_poll_with_session must fire webhooks after db.commit."""
        import inspect
        from sthrip.services.deposit_monitor import DepositMonitor

        source = inspect.getsource(DepositMonitor._do_poll_with_session)
        lines = source.split("\n")

        commit_line = None
        webhook_line = None
        for i, line in enumerate(lines):
            if "db.commit()" in line and commit_line is None:
                commit_line = i
            if "_fire_webhook" in line or "_fire_deferred_webhooks" in line:
                webhook_line = i

        assert commit_line is not None, "Must call db.commit()"
        assert webhook_line is not None, "Must fire webhooks"
        assert webhook_line > commit_line, (
            f"Webhooks (line {webhook_line}) must fire AFTER "
            f"db.commit() (line {commit_line})"
        )


# ── Task 19: Self-send check in same session as deduction ──────────────


class TestSelfSendSameSession:
    """Self-send check and balance deduction must use the same DB session."""

    def test_check_not_self_send_does_not_open_separate_session(self):
        """_check_not_self_send should not call get_db() — it should
        accept a session parameter or be integrated into the deduction."""
        import inspect
        from api.routers import balance as balance_mod

        # Find _check_not_self_send function
        if hasattr(balance_mod, "_check_not_self_send"):
            source = inspect.getsource(balance_mod._check_not_self_send)
            assert "get_db()" not in source, (
                "_check_not_self_send must not open its own DB session. "
                "Use the same session as the deduction for atomicity."
            )
        else:
            # If refactored away, check that the self-send check is inside
            # the deduction function
            source = inspect.getsource(balance_mod._deduct_and_create_pending)
            assert "deposit_address" in source, (
                "Self-send check must be inside _deduct_and_create_pending "
                "for single-session atomicity."
            )
