"""
Tests for the Recurring Payments feature.

TDD: These tests were written first (RED), then the implementation was added (GREEN).
"""

import uuid
import pytest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentBalance, AgentReputation, RecurringPayment,
)
from sthrip.db.enums import RecurringInterval


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[
        Agent.__table__,
        AgentReputation.__table__,
        AgentBalance.__table__,
        RecurringPayment.__table__,
    ])
    return engine


def _make_session(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return factory()


def _create_agent(db, name: str) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=name,
        api_key_hash="hash_" + name,
        is_active=True,
    )
    db.add(agent)
    db.flush()
    return agent


def _fund_agent(db, agent_id, amount: Decimal):
    from sthrip.db.balance_repo import BalanceRepository
    BalanceRepository(db).deposit(agent_id, amount)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def engine():
    return _make_engine()


@pytest.fixture
def db(engine):
    session = _make_session(engine)
    yield session
    session.close()


@pytest.fixture
def agent_a(db):
    return _create_agent(db, "agent_a")


@pytest.fixture
def agent_b(db):
    return _create_agent(db, "agent_b")


# ─────────────────────────────────────────────────────────────────────────────
# UNIT TESTS: RecurringPaymentRepository
# ─────────────────────────────────────────────────────────────────────────────

class TestRecurringPaymentRepository:
    """Unit tests for RecurringPaymentRepository."""

    def test_create_returns_recurring_payment(self, db, agent_a, agent_b):
        """create() stores a new RecurringPayment and returns the ORM object."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        repo = RecurringPaymentRepository(db)
        next_at = datetime.now(timezone.utc) + timedelta(hours=1)
        payment = repo.create(
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("0.5"),
            interval=RecurringInterval.HOURLY,
            max_payments=None,
            next_payment_at=next_at,
        )

        assert payment.id is not None
        assert payment.from_agent_id == agent_a.id
        assert payment.to_agent_id == agent_b.id
        assert payment.amount == Decimal("0.5")
        assert payment.interval == RecurringInterval.HOURLY
        assert payment.is_active is True
        assert payment.payments_made == 0
        assert payment.total_paid == Decimal("0")

    def test_get_by_id_returns_none_for_unknown(self, db):
        """get_by_id returns None when the ID does not exist."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        repo = RecurringPaymentRepository(db)
        result = repo.get_by_id(uuid.uuid4())
        assert result is None

    def test_get_by_id_returns_existing(self, db, agent_a, agent_b):
        """get_by_id returns the correct payment."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        repo = RecurringPaymentRepository(db)
        next_at = datetime.now(timezone.utc) + timedelta(hours=1)
        created = repo.create(
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.DAILY,
            max_payments=5,
            next_payment_at=next_at,
        )

        fetched = repo.get_by_id(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.max_payments == 5

    def test_get_due_payments_returns_overdue_active(self, db, agent_a, agent_b):
        """get_due_payments returns only active payments whose next_payment_at is past."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        repo = RecurringPaymentRepository(db)
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        future = datetime.now(timezone.utc) + timedelta(hours=2)

        due = repo.create(
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("0.1"),
            interval=RecurringInterval.HOURLY,
            max_payments=None,
            next_payment_at=past,
        )
        _not_due = repo.create(
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("0.1"),
            interval=RecurringInterval.DAILY,
            max_payments=None,
            next_payment_at=future,
        )

        due_list = repo.get_due_payments()
        ids = [p.id for p in due_list]
        assert due.id in ids
        assert _not_due.id not in ids

    def test_get_due_payments_excludes_inactive(self, db, agent_a, agent_b):
        """Cancelled (inactive) overdue payments are NOT returned."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        repo = RecurringPaymentRepository(db)
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        payment = repo.create(
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("0.1"),
            interval=RecurringInterval.HOURLY,
            max_payments=None,
            next_payment_at=past,
        )
        repo.cancel(payment.id)

        due_list = repo.get_due_payments()
        assert payment.id not in [p.id for p in due_list]

    def test_list_by_agent_finds_sender_and_receiver(self, db, agent_a, agent_b):
        """list_by_agent returns payments where the agent is sender or receiver."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        repo = RecurringPaymentRepository(db)
        next_at = datetime.now(timezone.utc) + timedelta(hours=1)
        p1 = repo.create(
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.DAILY,
            max_payments=None,
            next_payment_at=next_at,
        )

        items_a, total_a = repo.list_by_agent(agent_a.id)
        items_b, total_b = repo.list_by_agent(agent_b.id)

        assert total_a == 1
        assert p1.id in [x.id for x in items_a]
        assert total_b == 1
        assert p1.id in [x.id for x in items_b]

    def test_list_by_agent_pagination(self, db, agent_a, agent_b):
        """list_by_agent respects limit and offset."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        repo = RecurringPaymentRepository(db)
        next_at = datetime.now(timezone.utc) + timedelta(hours=1)
        for _ in range(5):
            repo.create(
                from_agent_id=agent_a.id,
                to_agent_id=agent_b.id,
                amount=Decimal("0.1"),
                interval=RecurringInterval.HOURLY,
                max_payments=None,
                next_payment_at=next_at,
            )

        items, total = repo.list_by_agent(agent_a.id, limit=2, offset=0)
        assert total == 5
        assert len(items) == 2

        items2, _ = repo.list_by_agent(agent_a.id, limit=2, offset=2)
        assert len(items2) == 2
        # Make sure pages are non-overlapping
        ids_page1 = {x.id for x in items}
        ids_page2 = {x.id for x in items2}
        assert ids_page1.isdisjoint(ids_page2)

    def test_record_payment_increments_counters(self, db, agent_a, agent_b):
        """record_payment advances payments_made, total_paid, and next_payment_at."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        repo = RecurringPaymentRepository(db)
        next_at = datetime.now(timezone.utc) + timedelta(hours=1)
        payment = repo.create(
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("2.0"),
            interval=RecurringInterval.HOURLY,
            max_payments=None,
            next_payment_at=next_at,
        )

        new_next = datetime.now(timezone.utc) + timedelta(hours=2)
        rows = repo.record_payment(payment.id, new_next)
        assert rows == 1

        db.expire(payment)
        refreshed = repo.get_by_id(payment.id)
        assert refreshed.payments_made == 1
        assert refreshed.total_paid == Decimal("2.0")
        assert refreshed.last_payment_at is not None

    def test_cancel_sets_inactive(self, db, agent_a, agent_b):
        """cancel() deactivates the payment and records cancelled_at."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        repo = RecurringPaymentRepository(db)
        next_at = datetime.now(timezone.utc) + timedelta(hours=1)
        payment = repo.create(
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.DAILY,
            max_payments=None,
            next_payment_at=next_at,
        )

        rows = repo.cancel(payment.id)
        assert rows == 1

        db.expire(payment)
        refreshed = repo.get_by_id(payment.id)
        assert refreshed.is_active is False
        assert refreshed.cancelled_at is not None

    def test_update_changes_amount(self, db, agent_a, agent_b):
        """update() can change the amount field."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        repo = RecurringPaymentRepository(db)
        next_at = datetime.now(timezone.utc) + timedelta(hours=1)
        payment = repo.create(
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.DAILY,
            max_payments=None,
            next_payment_at=next_at,
        )

        rows = repo.update(payment.id, amount=Decimal("3.0"))
        assert rows == 1

        db.expire(payment)
        refreshed = repo.get_by_id(payment.id)
        assert refreshed.amount == Decimal("3.0")

    def test_update_changes_interval(self, db, agent_a, agent_b):
        """update() can change the interval field."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        repo = RecurringPaymentRepository(db)
        next_at = datetime.now(timezone.utc) + timedelta(hours=1)
        payment = repo.create(
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.DAILY,
            max_payments=None,
            next_payment_at=next_at,
        )

        rows = repo.update(payment.id, interval=RecurringInterval.WEEKLY)
        assert rows == 1

        db.expire(payment)
        refreshed = repo.get_by_id(payment.id)
        assert refreshed.interval == RecurringInterval.WEEKLY

    def test_update_no_fields_returns_zero(self, db, agent_a, agent_b):
        """update() with no fields changes nothing and returns 0."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        repo = RecurringPaymentRepository(db)
        next_at = datetime.now(timezone.utc) + timedelta(hours=1)
        payment = repo.create(
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.DAILY,
            max_payments=None,
            next_payment_at=next_at,
        )

        rows = repo.update(payment.id)
        assert rows == 0


# ─────────────────────────────────────────────────────────────────────────────
# UNIT TESTS: RecurringService
# ─────────────────────────────────────────────────────────────────────────────

class TestRecurringService:
    """Unit tests for RecurringService business logic."""

    def test_interval_calculation_hourly(self):
        """_calculate_next_payment advances by 1 hour for HOURLY."""
        from sthrip.services.recurring_service import RecurringService

        svc = RecurringService()
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = svc._calculate_next_payment(RecurringInterval.HOURLY, base)
        assert result == base + timedelta(hours=1)

    def test_interval_calculation_daily(self):
        """_calculate_next_payment advances by 1 day for DAILY."""
        from sthrip.services.recurring_service import RecurringService

        svc = RecurringService()
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = svc._calculate_next_payment(RecurringInterval.DAILY, base)
        assert result == base + timedelta(days=1)

    def test_interval_calculation_weekly(self):
        """_calculate_next_payment advances by 7 days for WEEKLY."""
        from sthrip.services.recurring_service import RecurringService

        svc = RecurringService()
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = svc._calculate_next_payment(RecurringInterval.WEEKLY, base)
        assert result == base + timedelta(days=7)

    def test_interval_calculation_monthly(self):
        """_calculate_next_payment advances by 30 days for MONTHLY."""
        from sthrip.services.recurring_service import RecurringService

        svc = RecurringService()
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = svc._calculate_next_payment(RecurringInterval.MONTHLY, base)
        assert result == base + timedelta(days=30)

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_create_subscription_happy_path(
        self, mock_wh, mock_audit, db, agent_a, agent_b
    ):
        """create_subscription creates and returns a dict for valid input."""
        from sthrip.services.recurring_service import RecurringService

        svc = RecurringService()
        result = svc.create_subscription(
            db=db,
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("0.5"),
            interval=RecurringInterval.DAILY,
            max_payments=10,
        )

        assert result["from_agent_id"] == str(agent_a.id)
        assert result["to_agent_id"] == str(agent_b.id)
        assert result["amount"] == "0.5"
        assert result["interval"] == "daily"
        assert result["is_active"] is True
        assert mock_audit.called
        assert mock_wh.called

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_create_subscription_same_agent_raises(self, mock_wh, mock_audit, db, agent_a):
        """create_subscription raises ValueError when from == to."""
        from sthrip.services.recurring_service import RecurringService

        svc = RecurringService()
        with pytest.raises(ValueError, match="cannot subscribe to yourself"):
            svc.create_subscription(
                db=db,
                from_agent_id=agent_a.id,
                to_agent_id=agent_a.id,
                amount=Decimal("1.0"),
                interval=RecurringInterval.DAILY,
            )

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_create_subscription_zero_amount_raises(self, mock_wh, mock_audit, db, agent_a, agent_b):
        """create_subscription raises ValueError when amount <= 0."""
        from sthrip.services.recurring_service import RecurringService

        svc = RecurringService()
        with pytest.raises(ValueError, match="amount must be positive"):
            svc.create_subscription(
                db=db,
                from_agent_id=agent_a.id,
                to_agent_id=agent_b.id,
                amount=Decimal("0"),
                interval=RecurringInterval.DAILY,
            )

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_execute_payment_transfers_funds(self, mock_wh, mock_audit, db, agent_a, agent_b):
        """execute_due_payments deducts from sender and credits receiver."""
        from sthrip.services.recurring_service import RecurringService
        from sthrip.db.balance_repo import BalanceRepository

        _fund_agent(db, agent_a.id, Decimal("10.0"))

        svc = RecurringService()
        svc.create_subscription(
            db=db,
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.HOURLY,
        )

        # Force next_payment_at to the past
        from sthrip.db.models import RecurringPayment
        db.query(RecurringPayment).update({"next_payment_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
        db.flush()

        count = svc.execute_due_payments(db)
        assert count == 1

        bal_repo = BalanceRepository(db)
        sender_bal = bal_repo.get_available(agent_a.id)
        receiver_bal = bal_repo.get_available(agent_b.id)
        # 1% fee: sender loses 1.0, receiver gets 0.99
        assert sender_bal == Decimal("9.0")
        assert receiver_bal == Decimal("0.99")

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_execute_payment_insufficient_balance_skips(
        self, mock_wh, mock_audit, db, agent_a, agent_b
    ):
        """execute_due_payments skips payments where sender has insufficient funds."""
        from sthrip.services.recurring_service import RecurringService

        # agent_a has no funds (balance = 0)
        svc = RecurringService()
        svc.create_subscription(
            db=db,
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("5.0"),
            interval=RecurringInterval.DAILY,
        )

        from sthrip.db.models import RecurringPayment
        db.query(RecurringPayment).update({"next_payment_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
        db.flush()

        count = svc.execute_due_payments(db)
        assert count == 0

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_execute_payment_max_payments_cancels(
        self, mock_wh, mock_audit, db, agent_a, agent_b
    ):
        """execute_due_payments cancels the subscription after max_payments is reached."""
        from sthrip.services.recurring_service import RecurringService
        from sthrip.db.recurring_repo import RecurringPaymentRepository
        from sthrip.db.models import RecurringPayment

        _fund_agent(db, agent_a.id, Decimal("100.0"))

        svc = RecurringService()
        result = svc.create_subscription(
            db=db,
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.DAILY,
            max_payments=1,
        )
        payment_id = uuid.UUID(result["id"])

        # Simulate that 0 payments have been made, and set next_payment_at in the past
        db.query(RecurringPayment).filter(RecurringPayment.id == payment_id).update(
            {"next_payment_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        db.flush()

        count = svc.execute_due_payments(db)
        assert count == 1

        repo = RecurringPaymentRepository(db)
        db.expire_all()
        refreshed = repo.get_by_id(payment_id)
        # After 1 payment with max_payments=1, should be deactivated
        assert refreshed.is_active is False

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_cancel_subscription_by_participant(
        self, mock_wh, mock_audit, db, agent_a, agent_b
    ):
        """cancel_subscription succeeds when caller is a participant (from or to)."""
        from sthrip.services.recurring_service import RecurringService

        svc = RecurringService()
        result = svc.create_subscription(
            db=db,
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.DAILY,
        )
        payment_id = uuid.UUID(result["id"])

        # Cancel by sender
        cancelled = svc.cancel_subscription(db, payment_id, agent_a.id)
        assert cancelled["is_active"] is False

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_cancel_subscription_by_non_participant_raises(
        self, mock_wh, mock_audit, db, agent_a, agent_b
    ):
        """cancel_subscription raises PermissionError for a non-participant."""
        from sthrip.services.recurring_service import RecurringService

        svc = RecurringService()
        result = svc.create_subscription(
            db=db,
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.DAILY,
        )
        payment_id = uuid.UUID(result["id"])

        stranger = _create_agent(db, "stranger")
        with pytest.raises(PermissionError):
            svc.cancel_subscription(db, payment_id, stranger.id)

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_cancel_subscription_not_found_raises(self, mock_wh, mock_audit, db, agent_a):
        """cancel_subscription raises LookupError for an unknown ID."""
        from sthrip.services.recurring_service import RecurringService

        svc = RecurringService()
        with pytest.raises(LookupError):
            svc.cancel_subscription(db, uuid.uuid4(), agent_a.id)

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_update_subscription_by_sender(
        self, mock_wh, mock_audit, db, agent_a, agent_b
    ):
        """update_subscription succeeds when caller is the from_agent."""
        from sthrip.services.recurring_service import RecurringService

        svc = RecurringService()
        result = svc.create_subscription(
            db=db,
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.DAILY,
        )
        payment_id = uuid.UUID(result["id"])

        updated = svc.update_subscription(
            db, payment_id, agent_a.id, amount=Decimal("2.5")
        )
        assert updated["amount"] == "2.5"

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_update_subscription_by_receiver_raises(
        self, mock_wh, mock_audit, db, agent_a, agent_b
    ):
        """update_subscription raises PermissionError when caller is not the sender."""
        from sthrip.services.recurring_service import RecurringService

        svc = RecurringService()
        result = svc.create_subscription(
            db=db,
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.DAILY,
        )
        payment_id = uuid.UUID(result["id"])

        with pytest.raises(PermissionError):
            svc.update_subscription(db, payment_id, agent_b.id, amount=Decimal("2.0"))


# ─────────────────────────────────────────────────────────────────────────────
# API INTEGRATION TESTS
# ─────────────────────────────────────────────────────────────────────────────

# Modules to patch get_db in for subscription tests
_SUBSCRIPTION_DB_MODULES = [
    "sthrip.db.database",
    "sthrip.services.agent_registry",
    "sthrip.services.fee_collector",
    "sthrip.services.webhook_service",
    "api.main_v2",
    "api.deps",
    "api.routers.health",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.webhooks",
    "api.routers.spending_policy",
    "api.routers.webhook_endpoints",
    "api.routers.reputation",
    "api.routers.messages",
    "api.routers.multisig_escrow",
    "api.routers.escrow",
    "api.routers.sla",
    "api.routers.reviews",
    "api.routers.matchmaking",
    "api.routers.subscriptions",
]

_SUBSCRIPTION_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    RecurringPayment.__table__,
]


@pytest.fixture
def sub_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from sthrip.db.models import (
        HubRoute, FeeCollection, PendingWithdrawal, Transaction,
        SpendingPolicy, WebhookEndpoint, MessageRelay,
        EscrowDeal, EscrowMilestone, MultisigEscrow, MultisigRound,
        SLATemplate, SLAContract,
        AgentReview, AgentRatingSummary,
        MatchRequest,
    )
    all_tables = _SUBSCRIPTION_TABLES + [
        HubRoute.__table__,
        FeeCollection.__table__,
        PendingWithdrawal.__table__,
        Transaction.__table__,
        SpendingPolicy.__table__,
        WebhookEndpoint.__table__,
        MessageRelay.__table__,
        EscrowDeal.__table__,
        EscrowMilestone.__table__,
        MultisigEscrow.__table__,
        MultisigRound.__table__,
        SLATemplate.__table__,
        SLAContract.__table__,
        AgentReview.__table__,
        AgentRatingSummary.__table__,
        MatchRequest.__table__,
    ]
    Base.metadata.create_all(engine, tables=all_tables)
    return engine


@pytest.fixture
def sub_client(sub_engine):
    """FastAPI test client with subscriptions module patched."""
    import os
    import contextlib
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker
    from unittest.mock import patch, MagicMock

    session_factory = sessionmaker(bind=sub_engine, expire_on_commit=False)

    @contextmanager
    def get_test_db():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    mock_limiter = MagicMock()
    mock_limiter.check_rate_limit.return_value = None
    mock_limiter.check_ip_rate_limit.return_value = None
    mock_limiter.check_failed_auth.return_value = None
    mock_limiter.record_failed_auth.return_value = None
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-03-03T00:00:00",
        "checks": {},
    }
    mock_monitor.get_alerts.return_value = []
    mock_webhook_svc = MagicMock()
    mock_webhook_svc.get_delivery_stats.return_value = {"total": 0}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger"}))

        for mod in _SUBSCRIPTION_DB_MODULES:
            stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))
        stack.enter_context(patch("sthrip.db.database.create_tables"))

        _rate_limiter_modules = [
            "sthrip.services.rate_limiter",
            "api.main_v2",
            "api.deps",
            "api.routers.agents",
        ]
        for mod in _rate_limiter_modules:
            stack.enter_context(patch(f"{mod}.get_rate_limiter", return_value=mock_limiter))

        _audit_log_modules = [
            "api.main_v2",
            "api.deps",
            "api.routers.agents",
            "api.routers.payments",
            "api.routers.balance",
            "api.routers.admin",
        ]
        for mod in _audit_log_modules:
            stack.enter_context(patch(f"{mod}.audit_log"))

        stack.enter_context(
            patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor)
        )
        stack.enter_context(
            patch(
                "sthrip.services.monitoring.setup_default_monitoring",
                return_value=mock_monitor,
            )
        )
        stack.enter_context(
            patch(
                "sthrip.services.webhook_service.get_webhook_service",
                return_value=mock_webhook_svc,
            )
        )
        stack.enter_context(patch("sthrip.services.webhook_service.queue_webhook"))
        stack.enter_context(patch("sthrip.services.recurring_service.audit_log"))
        stack.enter_context(patch("sthrip.services.recurring_service.queue_webhook"))

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


def _register_agent(client, name: str) -> tuple[str, str]:
    """Register an agent and return (api_key, agent_id)."""
    resp = client.post(
        "/v2/agents/register",
        json={"agent_name": name},
    )
    assert resp.status_code == 201, f"Registration failed: {resp.text}"
    data = resp.json()
    return data["api_key"], data["agent_id"]


class TestSubscriptionsAPI:
    """API integration tests for /v2/subscriptions endpoints."""

    def test_api_create_subscription(self, sub_client):
        """POST /v2/subscriptions creates a subscription and returns 201."""
        key_a, _ = _register_agent(sub_client, "api_agent_a")
        _register_agent(sub_client, "api_agent_b")

        resp = sub_client.post(
            "/v2/subscriptions",
            json={
                "to_agent_name": "api_agent_b",
                "amount": "1.0",
                "interval": "daily",
            },
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["interval"] == "daily"
        assert data["is_active"] is True
        assert "id" in data

    def test_api_list_subscriptions(self, sub_client):
        """GET /v2/subscriptions returns a list of the agent's subscriptions."""
        key_a, _ = _register_agent(sub_client, "list_agent_a")
        _register_agent(sub_client, "list_agent_b")

        sub_client.post(
            "/v2/subscriptions",
            json={"to_agent_name": "list_agent_b", "amount": "0.5", "interval": "hourly"},
            headers={"Authorization": f"Bearer {key_a}"},
        )

        resp = sub_client.get(
            "/v2/subscriptions",
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "items" in data
        assert data["total"] >= 1

    def test_api_get_subscription_detail(self, sub_client):
        """GET /v2/subscriptions/{id} returns the subscription detail."""
        key_a, _ = _register_agent(sub_client, "detail_agent_a")
        _register_agent(sub_client, "detail_agent_b")

        create_resp = sub_client.post(
            "/v2/subscriptions",
            json={"to_agent_name": "detail_agent_b", "amount": "2.0", "interval": "weekly"},
            headers={"Authorization": f"Bearer {key_a}"},
        )
        sub_id = create_resp.json()["id"]

        resp = sub_client.get(
            f"/v2/subscriptions/{sub_id}",
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == sub_id

    def test_api_get_subscription_not_found(self, sub_client):
        """GET /v2/subscriptions/{id} returns 404 for unknown ID."""
        key_a, _ = _register_agent(sub_client, "notfound_agent")

        resp = sub_client.get(
            f"/v2/subscriptions/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 404

    def test_api_cancel_subscription(self, sub_client):
        """DELETE /v2/subscriptions/{id} cancels the subscription."""
        key_a, _ = _register_agent(sub_client, "cancel_agent_a")
        _register_agent(sub_client, "cancel_agent_b")

        create_resp = sub_client.post(
            "/v2/subscriptions",
            json={"to_agent_name": "cancel_agent_b", "amount": "1.0", "interval": "daily"},
            headers={"Authorization": f"Bearer {key_a}"},
        )
        sub_id = create_resp.json()["id"]

        resp = sub_client.delete(
            f"/v2/subscriptions/{sub_id}",
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_active"] is False

    def test_api_update_subscription(self, sub_client):
        """PATCH /v2/subscriptions/{id} updates amount and interval."""
        key_a, _ = _register_agent(sub_client, "update_agent_a")
        _register_agent(sub_client, "update_agent_b")

        create_resp = sub_client.post(
            "/v2/subscriptions",
            json={"to_agent_name": "update_agent_b", "amount": "1.0", "interval": "daily"},
            headers={"Authorization": f"Bearer {key_a}"},
        )
        sub_id = create_resp.json()["id"]

        resp = sub_client.patch(
            f"/v2/subscriptions/{sub_id}",
            json={"amount": "3.0", "interval": "weekly"},
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["amount"] == "3.0"
        assert data["interval"] == "weekly"

    def test_api_create_subscription_same_agent(self, sub_client):
        """POST /v2/subscriptions returns 400 when subscribing to yourself."""
        key_a, _ = _register_agent(sub_client, "self_sub_agent")

        resp = sub_client.post(
            "/v2/subscriptions",
            json={"to_agent_name": "self_sub_agent", "amount": "1.0", "interval": "daily"},
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 400

    def test_api_create_subscription_unauthenticated(self, sub_client):
        """POST /v2/subscriptions returns 401 without credentials."""
        resp = sub_client.post(
            "/v2/subscriptions",
            json={"to_agent_name": "someone", "amount": "1.0", "interval": "daily"},
        )
        assert resp.status_code == 401
