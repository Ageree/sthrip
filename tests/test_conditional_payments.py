"""
Tests for ConditionalPaymentService -- TDD (RED) phase.

Tests cover: create with each condition type, time_lock triggers correctly,
cancel refunds, expiry refunds, insufficient balance rejected,
webhook trigger, execute_payment, and evaluate_conditions background task.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base,
    Agent,
    AgentReputation,
    AgentBalance,
    ConditionalPayment,
    ConditionalPaymentState,
    EscrowDeal,
    EscrowStatus,
)


# ---------------------------------------------------------------------------
# SQLite datetime compatibility
# ---------------------------------------------------------------------------

def _naive_utc_now() -> datetime:
    """Return current UTC time as a naive datetime (no tzinfo)."""
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# In-memory database fixture
# ---------------------------------------------------------------------------

_COND_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    ConditionalPayment.__table__,
    EscrowDeal.__table__,
]


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_COND_TABLES)
    return engine


@pytest.fixture
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(db, name: str, balance: Decimal = Decimal("100")) -> Agent:
    """Insert an agent with reputation and balance."""
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=name,
        api_key_hash="hash_" + name,
        is_active=True,
    )
    db.add(agent)
    db.flush()
    rep = AgentReputation(agent_id=agent.id)
    bal = AgentBalance(agent_id=agent.id, token="XMR", available=balance)
    db.add_all([rep, bal])
    db.flush()
    return agent


def _make_completed_escrow(db, buyer, seller) -> EscrowDeal:
    """Create a completed escrow deal for testing escrow_completed condition."""
    import hashlib
    import secrets
    raw = f"{buyer.id}{seller.id}1.0{_naive_utc_now().isoformat()}{secrets.token_hex(4)}"
    deal_hash = hashlib.sha256(raw.encode()).hexdigest()
    deal = EscrowDeal(
        id=uuid.uuid4(),
        deal_hash=deal_hash,
        buyer_id=buyer.id,
        seller_id=seller.id,
        amount=Decimal("1.0"),
        token="XMR",
        description="test escrow",
        fee_percent=Decimal("0.01"),
        fee_amount=Decimal("0"),
        status=EscrowStatus.COMPLETED,
        accept_timeout_hours=24,
        delivery_timeout_hours=48,
        review_timeout_hours=24,
        accept_deadline=_naive_utc_now() + timedelta(hours=24),
    )
    db.add(deal)
    db.flush()
    return deal


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestConditionalPaymentService:
    """Unit tests for ConditionalPaymentService."""

    # ------------------------------------------------------------------
    # create_conditional -- time_lock
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_create_time_lock(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "sender_a")
        receiver = _make_agent(db_session, "receiver_b")

        release_at = (_naive_utc_now() + timedelta(hours=2)).isoformat()
        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("10"),
            currency="XMR",
            condition_type="time_lock",
            condition_config={"release_at": release_at},
        )

        assert result["state"] == "pending"
        assert result["condition_type"] == "time_lock"
        assert result["amount"] == "10"
        assert result["from_agent_id"] == str(sender.id)
        assert result["to_agent_id"] == str(receiver.id)

        # Sender balance should be reduced
        bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == sender.id,
        ).first()
        assert bal.available == Decimal("90")

    # ------------------------------------------------------------------
    # create_conditional -- escrow_completed
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_create_escrow_completed(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "sender_c")
        receiver = _make_agent(db_session, "receiver_d")

        escrow_id = str(uuid.uuid4())
        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("5"),
            currency="XMR",
            condition_type="escrow_completed",
            condition_config={
                "escrow_id": escrow_id,
                "required_status": "completed",
            },
        )

        assert result["state"] == "pending"
        assert result["condition_type"] == "escrow_completed"

    # ------------------------------------------------------------------
    # create_conditional -- balance_threshold
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_create_balance_threshold(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "sender_e")
        receiver = _make_agent(db_session, "receiver_f")

        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("5"),
            currency="XMR",
            condition_type="balance_threshold",
            condition_config={
                "agent_id": str(receiver.id),
                "threshold": "50",
            },
        )

        assert result["state"] == "pending"
        assert result["condition_type"] == "balance_threshold"

    # ------------------------------------------------------------------
    # create_conditional -- webhook
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_create_webhook(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "sender_g")
        receiver = _make_agent(db_session, "receiver_h")

        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("5"),
            currency="XMR",
            condition_type="webhook",
            condition_config={"callback_url": "https://example.com/hook"},
        )

        assert result["state"] == "pending"
        assert result["condition_type"] == "webhook"

    # ------------------------------------------------------------------
    # create_conditional -- self-payment rejected
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_create_self_payment_rejected(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "self_payer")

        with pytest.raises(ValueError, match="different"):
            ConditionalPaymentService.create_conditional(
                db=db_session,
                from_agent_id=sender.id,
                to_agent_id=sender.id,
                amount=Decimal("5"),
                currency="XMR",
                condition_type="time_lock",
                condition_config={"release_at": _naive_utc_now().isoformat()},
            )

    # ------------------------------------------------------------------
    # create_conditional -- invalid condition type
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_create_invalid_condition_type(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "sender_inv")
        receiver = _make_agent(db_session, "receiver_inv")

        with pytest.raises(ValueError, match="condition_type"):
            ConditionalPaymentService.create_conditional(
                db=db_session,
                from_agent_id=sender.id,
                to_agent_id=receiver.id,
                amount=Decimal("5"),
                currency="XMR",
                condition_type="invalid_type",
                condition_config={},
            )

    # ------------------------------------------------------------------
    # create_conditional -- missing required config fields
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_create_missing_config_fields(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "sender_miss")
        receiver = _make_agent(db_session, "receiver_miss")

        # time_lock requires release_at
        with pytest.raises(ValueError, match="release_at"):
            ConditionalPaymentService.create_conditional(
                db=db_session,
                from_agent_id=sender.id,
                to_agent_id=receiver.id,
                amount=Decimal("5"),
                currency="XMR",
                condition_type="time_lock",
                condition_config={},
            )

    # ------------------------------------------------------------------
    # create_conditional -- insufficient balance
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_create_insufficient_balance(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "poor_sender", balance=Decimal("1"))
        receiver = _make_agent(db_session, "receiver_rich")

        with pytest.raises(ValueError, match="[Ii]nsufficient"):
            ConditionalPaymentService.create_conditional(
                db=db_session,
                from_agent_id=sender.id,
                to_agent_id=receiver.id,
                amount=Decimal("50"),
                currency="XMR",
                condition_type="time_lock",
                condition_config={
                    "release_at": (_naive_utc_now() + timedelta(hours=1)).isoformat(),
                },
            )

    # ------------------------------------------------------------------
    # cancel -- refunds sender
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_cancel_refunds_sender(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "cancel_sender")
        receiver = _make_agent(db_session, "cancel_receiver")

        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("20"),
            currency="XMR",
            condition_type="time_lock",
            condition_config={
                "release_at": (_naive_utc_now() + timedelta(hours=10)).isoformat(),
            },
        )

        payment_id = uuid.UUID(result["id"])
        cancelled = ConditionalPaymentService.cancel(
            db=db_session,
            agent_id=sender.id,
            payment_id=payment_id,
        )
        assert cancelled["state"] == "cancelled"

        # Balance should be fully refunded
        bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == sender.id,
        ).first()
        assert bal.available == Decimal("100")

    # ------------------------------------------------------------------
    # cancel -- non-sender cannot cancel
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_cancel_by_non_sender_rejected(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "auth_sender")
        receiver = _make_agent(db_session, "auth_receiver")

        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("10"),
            currency="XMR",
            condition_type="webhook",
            condition_config={"callback_url": "https://example.com/hook"},
        )

        payment_id = uuid.UUID(result["id"])
        with pytest.raises(PermissionError):
            ConditionalPaymentService.cancel(
                db=db_session,
                agent_id=receiver.id,
                payment_id=payment_id,
            )

    # ------------------------------------------------------------------
    # cancel -- non-PENDING cannot cancel
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_cancel_already_executed_rejected(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "exec_sender")
        receiver = _make_agent(db_session, "exec_receiver")

        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("10"),
            currency="XMR",
            condition_type="webhook",
            condition_config={"callback_url": "https://example.com/hook"},
        )

        # Manually transition to EXECUTED
        payment_id = uuid.UUID(result["id"])
        cp = db_session.query(ConditionalPayment).get(payment_id)
        cp.state = ConditionalPaymentState.TRIGGERED
        db_session.flush()
        from sthrip.db.conditional_payment_repo import ConditionalPaymentRepository
        ConditionalPaymentRepository(db_session).execute(payment_id)
        db_session.flush()

        with pytest.raises(ValueError, match="[Cc]annot cancel"):
            ConditionalPaymentService.cancel(
                db=db_session,
                agent_id=sender.id,
                payment_id=payment_id,
            )

    # ------------------------------------------------------------------
    # expire_stale -- refunds expired payments
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_expire_stale_refunds(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "exp_sender")
        receiver = _make_agent(db_session, "exp_receiver")

        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("15"),
            currency="XMR",
            condition_type="time_lock",
            condition_config={
                "release_at": (_naive_utc_now() + timedelta(hours=100)).isoformat(),
            },
            expires_hours=0,  # already expired
        )

        # Manually set expires_at to past
        payment_id = uuid.UUID(result["id"])
        cp = db_session.query(ConditionalPayment).get(payment_id)
        cp.expires_at = _naive_utc_now() - timedelta(hours=1)
        db_session.flush()

        count = ConditionalPaymentService.expire_stale(db=db_session)
        assert count >= 1

        # Balance refunded
        bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == sender.id,
        ).first()
        assert bal.available == Decimal("100")

        # State is EXPIRED
        cp = db_session.query(ConditionalPayment).get(payment_id)
        state_val = cp.state.value if hasattr(cp.state, "value") else cp.state
        assert state_val == "expired"

    # ------------------------------------------------------------------
    # trigger_webhook -- transitions PENDING -> TRIGGERED -> EXECUTED
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_trigger_webhook_executes(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "wh_sender")
        receiver = _make_agent(db_session, "wh_receiver")

        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("10"),
            currency="XMR",
            condition_type="webhook",
            condition_config={"callback_url": "https://example.com/hook"},
        )

        payment_id = uuid.UUID(result["id"])
        triggered = ConditionalPaymentService.trigger_webhook(
            db=db_session,
            payment_id=payment_id,
            agent_id=sender.id,
        )

        assert triggered["state"] == "executed"

        # Receiver credited
        recv_bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == receiver.id,
        ).first()
        assert recv_bal.available == Decimal("110")

    # ------------------------------------------------------------------
    # trigger_webhook -- wrong agent rejected
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_trigger_webhook_wrong_agent(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "wh_wrong_sender")
        receiver = _make_agent(db_session, "wh_wrong_receiver")

        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("10"),
            currency="XMR",
            condition_type="webhook",
            condition_config={"callback_url": "https://example.com/hook"},
        )

        payment_id = uuid.UUID(result["id"])
        with pytest.raises(PermissionError):
            ConditionalPaymentService.trigger_webhook(
                db=db_session,
                payment_id=payment_id,
                agent_id=receiver.id,
            )

    # ------------------------------------------------------------------
    # evaluate_conditions -- time_lock met
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_evaluate_conditions_time_lock_met(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "eval_sender")
        receiver = _make_agent(db_session, "eval_receiver")

        # Create with release_at in the past
        release_at = (_naive_utc_now() - timedelta(hours=1)).isoformat()
        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("10"),
            currency="XMR",
            condition_type="time_lock",
            condition_config={"release_at": release_at},
        )

        count = ConditionalPaymentService.evaluate_conditions(db=db_session)
        assert count >= 1

        # Receiver should be credited
        recv_bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == receiver.id,
        ).first()
        assert recv_bal.available == Decimal("110")

        # Payment should be EXECUTED
        payment_id = uuid.UUID(result["id"])
        cp = db_session.query(ConditionalPayment).get(payment_id)
        state_val = cp.state.value if hasattr(cp.state, "value") else cp.state
        assert state_val == "executed"

    # ------------------------------------------------------------------
    # evaluate_conditions -- time_lock not yet met
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_evaluate_conditions_time_lock_not_met(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "eval_future_sender")
        receiver = _make_agent(db_session, "eval_future_receiver")

        release_at = (_naive_utc_now() + timedelta(hours=10)).isoformat()
        ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("10"),
            currency="XMR",
            condition_type="time_lock",
            condition_config={"release_at": release_at},
        )

        count = ConditionalPaymentService.evaluate_conditions(db=db_session)
        assert count == 0

        # Receiver balance unchanged
        recv_bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == receiver.id,
        ).first()
        assert recv_bal.available == Decimal("100")

    # ------------------------------------------------------------------
    # evaluate_conditions -- escrow_completed condition met
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_evaluate_conditions_escrow_completed_met(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "eval_escrow_sender")
        receiver = _make_agent(db_session, "eval_escrow_receiver")

        deal = _make_completed_escrow(db_session, sender, receiver)

        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("5"),
            currency="XMR",
            condition_type="escrow_completed",
            condition_config={
                "escrow_id": str(deal.id),
                "required_status": "completed",
            },
        )

        count = ConditionalPaymentService.evaluate_conditions(db=db_session)
        assert count >= 1

        payment_id = uuid.UUID(result["id"])
        cp = db_session.query(ConditionalPayment).get(payment_id)
        state_val = cp.state.value if hasattr(cp.state, "value") else cp.state
        assert state_val == "executed"

    # ------------------------------------------------------------------
    # evaluate_conditions -- balance_threshold condition met
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_evaluate_conditions_balance_threshold_met(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "eval_bal_sender")
        receiver = _make_agent(db_session, "eval_bal_receiver", balance=Decimal("10"))

        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("5"),
            currency="XMR",
            condition_type="balance_threshold",
            condition_config={
                "agent_id": str(receiver.id),
                "threshold": "50",
            },
        )

        # receiver has balance=10 which is below threshold=50, so condition met
        count = ConditionalPaymentService.evaluate_conditions(db=db_session)
        assert count >= 1

        payment_id = uuid.UUID(result["id"])
        cp = db_session.query(ConditionalPayment).get(payment_id)
        state_val = cp.state.value if hasattr(cp.state, "value") else cp.state
        assert state_val == "executed"

    # ------------------------------------------------------------------
    # evaluate_conditions -- webhook type skipped
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_evaluate_conditions_skips_webhook(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "eval_wh_sender")
        receiver = _make_agent(db_session, "eval_wh_receiver")

        ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("5"),
            currency="XMR",
            condition_type="webhook",
            condition_config={"callback_url": "https://example.com/hook"},
        )

        count = ConditionalPaymentService.evaluate_conditions(db=db_session)
        assert count == 0

    # ------------------------------------------------------------------
    # execute_payment -- credits recipient
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_execute_payment_credits_recipient(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "exec_pay_sender")
        receiver = _make_agent(db_session, "exec_pay_receiver")

        result = ConditionalPaymentService.create_conditional(
            db=db_session,
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount=Decimal("25"),
            currency="XMR",
            condition_type="webhook",
            condition_config={"callback_url": "https://example.com/hook"},
        )

        # Manually trigger
        payment_id = uuid.UUID(result["id"])
        from sthrip.db.conditional_payment_repo import ConditionalPaymentRepository
        repo = ConditionalPaymentRepository(db_session)
        repo.trigger(payment_id)
        db_session.flush()

        executed = ConditionalPaymentService.execute_payment(
            db=db_session,
            payment_id=payment_id,
        )

        assert executed["state"] == "executed"

        recv_bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == receiver.id,
        ).first()
        assert recv_bal.available == Decimal("125")

    # ------------------------------------------------------------------
    # execute_payment -- not found
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_execute_payment_not_found(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        with pytest.raises(LookupError):
            ConditionalPaymentService.execute_payment(
                db=db_session,
                payment_id=uuid.uuid4(),
            )

    # ------------------------------------------------------------------
    # cancel -- not found
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_cancel_not_found(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        with pytest.raises(LookupError):
            ConditionalPaymentService.cancel(
                db=db_session,
                agent_id=uuid.uuid4(),
                payment_id=uuid.uuid4(),
            )

    # ------------------------------------------------------------------
    # create_conditional -- escrow_completed missing escrow_id
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_create_escrow_completed_missing_fields(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "missing_escrow_sender")
        receiver = _make_agent(db_session, "missing_escrow_receiver")

        with pytest.raises(ValueError, match="escrow_id"):
            ConditionalPaymentService.create_conditional(
                db=db_session,
                from_agent_id=sender.id,
                to_agent_id=receiver.id,
                amount=Decimal("5"),
                currency="XMR",
                condition_type="escrow_completed",
                condition_config={"required_status": "completed"},
            )

    # ------------------------------------------------------------------
    # create_conditional -- balance_threshold missing fields
    # ------------------------------------------------------------------

    @patch("sthrip.services.conditional_payment_service.audit_log")
    @patch("sthrip.services.conditional_payment_service.queue_webhook")
    def test_create_balance_threshold_missing_fields(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.conditional_payment_service import ConditionalPaymentService

        sender = _make_agent(db_session, "missing_bal_sender")
        receiver = _make_agent(db_session, "missing_bal_receiver")

        with pytest.raises(ValueError, match="agent_id"):
            ConditionalPaymentService.create_conditional(
                db=db_session,
                from_agent_id=sender.id,
                to_agent_id=receiver.id,
                amount=Decimal("5"),
                currency="XMR",
                condition_type="balance_threshold",
                condition_config={"threshold": "50"},
            )
