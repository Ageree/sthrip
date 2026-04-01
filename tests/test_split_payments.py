"""
Tests for SplitPaymentService -- TDD (RED) phase.

Tests cover: atomic success, one invalid recipient fails entire batch,
insufficient balance, zero recipients rejected, memo propagation.
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
    Transaction,
)


# ---------------------------------------------------------------------------
# In-memory database fixture
# ---------------------------------------------------------------------------

_SPLIT_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    Transaction.__table__,
]


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_SPLIT_TABLES)
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


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestSplitPaymentService:
    """Unit tests for SplitPaymentService."""

    # ------------------------------------------------------------------
    # pay_split -- atomic success with multiple recipients
    # ------------------------------------------------------------------

    @patch("sthrip.services.split_payment_service.audit_log")
    @patch("sthrip.services.split_payment_service.queue_webhook")
    def test_pay_split_success(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.split_payment_service import SplitPaymentService

        sender = _make_agent(db_session, "split_sender")
        recv_a = _make_agent(db_session, "recv_a")
        recv_b = _make_agent(db_session, "recv_b")

        recipients = [
            {"agent_name": "recv_a", "amount": Decimal("10")},
            {"agent_name": "recv_b", "amount": Decimal("20")},
        ]

        results = SplitPaymentService.pay_split(
            db=db_session,
            from_agent_id=sender.id,
            recipients=recipients,
            currency="XMR",
            memo="test split",
        )

        assert len(results) == 2

        # Sender balance should be reduced by total (30)
        sender_bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == sender.id,
        ).first()
        assert sender_bal.available == Decimal("70")

        # Each recipient credited
        recv_a_bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == recv_a.id,
        ).first()
        assert recv_a_bal.available == Decimal("110")

        recv_b_bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == recv_b.id,
        ).first()
        assert recv_b_bal.available == Decimal("120")

    # ------------------------------------------------------------------
    # pay_split -- result contains expected fields
    # ------------------------------------------------------------------

    @patch("sthrip.services.split_payment_service.audit_log")
    @patch("sthrip.services.split_payment_service.queue_webhook")
    def test_pay_split_result_fields(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.split_payment_service import SplitPaymentService

        sender = _make_agent(db_session, "fields_sender")
        recv = _make_agent(db_session, "fields_recv")

        recipients = [
            {"agent_name": "fields_recv", "amount": Decimal("5")},
        ]

        results = SplitPaymentService.pay_split(
            db=db_session,
            from_agent_id=sender.id,
            recipients=recipients,
            currency="XMR",
            memo="memo test",
        )

        assert len(results) == 1
        receipt = results[0]
        assert "tx_hash" in receipt
        assert "to_agent_id" in receipt
        assert "amount" in receipt
        assert receipt["amount"] == "5"

    # ------------------------------------------------------------------
    # pay_split -- one invalid recipient fails entire batch
    # ------------------------------------------------------------------

    @patch("sthrip.services.split_payment_service.audit_log")
    @patch("sthrip.services.split_payment_service.queue_webhook")
    def test_pay_split_invalid_recipient_fails_all(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.split_payment_service import SplitPaymentService

        sender = _make_agent(db_session, "batch_fail_sender")
        recv_ok = _make_agent(db_session, "batch_recv_ok")

        recipients = [
            {"agent_name": "batch_recv_ok", "amount": Decimal("10")},
            {"agent_name": "nonexistent_agent", "amount": Decimal("5")},
        ]

        with pytest.raises(LookupError, match="not found"):
            SplitPaymentService.pay_split(
                db=db_session,
                from_agent_id=sender.id,
                recipients=recipients,
                currency="XMR",
            )

        # Sender balance should be unchanged (rollback)
        sender_bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == sender.id,
        ).first()
        assert sender_bal.available == Decimal("100")

        # Good recipient should NOT be credited
        recv_ok_bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == recv_ok.id,
        ).first()
        assert recv_ok_bal.available == Decimal("100")

    # ------------------------------------------------------------------
    # pay_split -- insufficient balance
    # ------------------------------------------------------------------

    @patch("sthrip.services.split_payment_service.audit_log")
    @patch("sthrip.services.split_payment_service.queue_webhook")
    def test_pay_split_insufficient_balance(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.split_payment_service import SplitPaymentService

        sender = _make_agent(db_session, "poor_split_sender", balance=Decimal("5"))
        recv = _make_agent(db_session, "poor_split_recv")

        recipients = [
            {"agent_name": "poor_split_recv", "amount": Decimal("50")},
        ]

        with pytest.raises(ValueError, match="[Ii]nsufficient"):
            SplitPaymentService.pay_split(
                db=db_session,
                from_agent_id=sender.id,
                recipients=recipients,
                currency="XMR",
            )

    # ------------------------------------------------------------------
    # pay_split -- zero recipients rejected
    # ------------------------------------------------------------------

    @patch("sthrip.services.split_payment_service.audit_log")
    @patch("sthrip.services.split_payment_service.queue_webhook")
    def test_pay_split_zero_recipients_rejected(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.split_payment_service import SplitPaymentService

        sender = _make_agent(db_session, "zero_split_sender")

        with pytest.raises(ValueError, match="[Rr]ecipient"):
            SplitPaymentService.pay_split(
                db=db_session,
                from_agent_id=sender.id,
                recipients=[],
                currency="XMR",
            )

    # ------------------------------------------------------------------
    # pay_split -- creates Transaction records
    # ------------------------------------------------------------------

    @patch("sthrip.services.split_payment_service.audit_log")
    @patch("sthrip.services.split_payment_service.queue_webhook")
    def test_pay_split_creates_transactions(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.split_payment_service import SplitPaymentService

        sender = _make_agent(db_session, "tx_sender")
        recv = _make_agent(db_session, "tx_recv")

        recipients = [
            {"agent_name": "tx_recv", "amount": Decimal("10")},
        ]

        SplitPaymentService.pay_split(
            db=db_session,
            from_agent_id=sender.id,
            recipients=recipients,
            currency="XMR",
            memo="split tx",
        )

        txs = db_session.query(Transaction).filter(
            Transaction.from_agent_id == sender.id,
        ).all()
        assert len(txs) == 1
        assert txs[0].amount == Decimal("10")
        assert txs[0].memo == "split tx"

    # ------------------------------------------------------------------
    # pay_split -- self-payment in recipients
    # ------------------------------------------------------------------

    @patch("sthrip.services.split_payment_service.audit_log")
    @patch("sthrip.services.split_payment_service.queue_webhook")
    def test_pay_split_self_payment_rejected(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.split_payment_service import SplitPaymentService

        sender = _make_agent(db_session, "self_split")

        recipients = [
            {"agent_name": "self_split", "amount": Decimal("10")},
        ]

        with pytest.raises(ValueError, match="[Cc]annot.*self|[Ss]elf"):
            SplitPaymentService.pay_split(
                db=db_session,
                from_agent_id=sender.id,
                recipients=recipients,
                currency="XMR",
            )

    # ------------------------------------------------------------------
    # pay_split -- three recipients
    # ------------------------------------------------------------------

    @patch("sthrip.services.split_payment_service.audit_log")
    @patch("sthrip.services.split_payment_service.queue_webhook")
    def test_pay_split_three_recipients(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.split_payment_service import SplitPaymentService

        sender = _make_agent(db_session, "three_sender", balance=Decimal("200"))
        recv_x = _make_agent(db_session, "recv_x")
        recv_y = _make_agent(db_session, "recv_y")
        recv_z = _make_agent(db_session, "recv_z")

        recipients = [
            {"agent_name": "recv_x", "amount": Decimal("30")},
            {"agent_name": "recv_y", "amount": Decimal("40")},
            {"agent_name": "recv_z", "amount": Decimal("50")},
        ]

        results = SplitPaymentService.pay_split(
            db=db_session,
            from_agent_id=sender.id,
            recipients=recipients,
            currency="XMR",
        )

        assert len(results) == 3

        sender_bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == sender.id,
        ).first()
        assert sender_bal.available == Decimal("80")  # 200 - 120

    # ------------------------------------------------------------------
    # pay_split -- duplicate recipient names
    # ------------------------------------------------------------------

    @patch("sthrip.services.split_payment_service.audit_log")
    @patch("sthrip.services.split_payment_service.queue_webhook")
    def test_pay_split_duplicate_recipients(self, mock_webhook, mock_audit, db_session):
        from sthrip.services.split_payment_service import SplitPaymentService

        sender = _make_agent(db_session, "dup_sender")
        recv = _make_agent(db_session, "dup_recv")

        recipients = [
            {"agent_name": "dup_recv", "amount": Decimal("10")},
            {"agent_name": "dup_recv", "amount": Decimal("15")},
        ]

        # Duplicate names should still work -- two separate payments to same agent
        results = SplitPaymentService.pay_split(
            db=db_session,
            from_agent_id=sender.id,
            recipients=recipients,
            currency="XMR",
        )

        assert len(results) == 2

        recv_bal = db_session.query(AgentBalance).filter(
            AgentBalance.agent_id == recv.id,
        ).first()
        assert recv_bal.available == Decimal("125")  # 100 + 10 + 15
