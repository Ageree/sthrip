"""Tests for balance repository"""
import pytest
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import create_engine, String, event, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, PendingWithdrawal,
    AgentTier, RateLimitTier, PrivacyLevel,
)
from sthrip.db.repository import BalanceRepository

# Only create the tables we need for testing (avoids INET type issue with SQLite)
_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    PendingWithdrawal.__table__,
]


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def agent(db_session):
    agent = Agent(
        agent_name="test-agent",
        api_key_hash="testhash",
        tier=AgentTier.FREE,
        rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.MEDIUM,
        is_active=True,
        xmr_address="test_address_123"
    )
    db_session.add(agent)
    db_session.flush()
    return agent


class TestBalanceRepository:
    def test_get_or_create_new(self, db_session, agent):
        repo = BalanceRepository(db_session)
        balance = repo.get_or_create(agent.id)
        assert balance.agent_id == agent.id
        assert (balance.available or 0) == 0
        assert balance.token == "XMR"

    def test_deposit(self, db_session, agent):
        repo = BalanceRepository(db_session)
        balance = repo.deposit(agent.id, Decimal("5.0"))
        assert balance.available == Decimal("5.0")
        assert balance.total_deposited == Decimal("5.0")

    def test_multiple_deposits(self, db_session, agent):
        repo = BalanceRepository(db_session)
        repo.deposit(agent.id, Decimal("3.0"))
        balance = repo.deposit(agent.id, Decimal("2.0"))
        assert balance.available == Decimal("5.0")
        assert balance.total_deposited == Decimal("5.0")

    def test_deduct(self, db_session, agent):
        repo = BalanceRepository(db_session)
        repo.deposit(agent.id, Decimal("10.0"))
        balance = repo.deduct(agent.id, Decimal("3.0"))
        assert balance.available == Decimal("7.0")

    def test_deduct_insufficient(self, db_session, agent):
        repo = BalanceRepository(db_session)
        repo.deposit(agent.id, Decimal("1.0"))
        with pytest.raises(ValueError, match="Insufficient balance"):
            repo.deduct(agent.id, Decimal("5.0"))

    def test_credit(self, db_session, agent):
        repo = BalanceRepository(db_session)
        balance = repo.credit(agent.id, Decimal("7.5"))
        assert balance.available == Decimal("7.5")

    def test_get_available(self, db_session, agent):
        repo = BalanceRepository(db_session)
        assert repo.get_available(agent.id) == Decimal("0")
        repo.deposit(agent.id, Decimal("4.2"))
        assert repo.get_available(agent.id) == Decimal("4.2")

    def test_full_hub_routing_flow(self, db_session, agent):
        recipient = Agent(
            agent_name="recipient",
            api_key_hash="recipienthash",
            tier=AgentTier.FREE,
            rate_limit_tier=RateLimitTier.STANDARD,
            privacy_level=PrivacyLevel.MEDIUM,
            is_active=True,
            xmr_address="recipient_address"
        )
        db_session.add(recipient)
        db_session.flush()

        repo = BalanceRepository(db_session)
        repo.deposit(agent.id, Decimal("10.0"))

        amount = Decimal("5.0")
        fee = Decimal("0.005")
        total = amount + fee

        repo.deduct(agent.id, total)
        repo.credit(recipient.id, amount)

        assert repo.get_available(agent.id) == Decimal("4.995")
        assert repo.get_available(recipient.id) == Decimal("5.0")


class TestAddPending:
    """Tests for BalanceRepository.add_pending (HIGH-7)."""

    def test_add_pending_creates_balance_if_needed(self, db_session, agent):
        repo = BalanceRepository(db_session)
        balance = repo.add_pending(agent.id, Decimal("2.5"))
        assert balance.pending == Decimal("2.5")

    def test_add_pending_accumulates(self, db_session, agent):
        repo = BalanceRepository(db_session)
        repo.add_pending(agent.id, Decimal("1.0"))
        balance = repo.add_pending(agent.id, Decimal("3.0"))
        assert balance.pending == Decimal("4.0")

    def test_add_pending_does_not_affect_available(self, db_session, agent):
        repo = BalanceRepository(db_session)
        repo.deposit(agent.id, Decimal("10.0"))
        repo.add_pending(agent.id, Decimal("5.0"))
        assert repo.get_available(agent.id) == Decimal("10.0")

    def test_add_pending_updates_timestamp(self, db_session, agent):
        repo = BalanceRepository(db_session)
        balance = repo.add_pending(agent.id, Decimal("1.0"))
        assert balance.updated_at is not None


class TestClearPendingOnConfirm:
    """Tests for BalanceRepository.clear_pending_on_confirm (HIGH-7)."""

    def test_clears_pending_amount(self, db_session, agent):
        repo = BalanceRepository(db_session)
        repo.add_pending(agent.id, Decimal("5.0"))
        balance = repo.clear_pending_on_confirm(agent.id, Decimal("5.0"))
        assert balance.pending == Decimal("0")

    def test_partial_clear(self, db_session, agent):
        repo = BalanceRepository(db_session)
        repo.add_pending(agent.id, Decimal("10.0"))
        balance = repo.clear_pending_on_confirm(agent.id, Decimal("3.0"))
        assert balance.pending == Decimal("7.0")

    def test_clear_more_than_pending_floors_to_zero(self, db_session, agent):
        repo = BalanceRepository(db_session)
        repo.add_pending(agent.id, Decimal("2.0"))
        balance = repo.clear_pending_on_confirm(agent.id, Decimal("5.0"))
        assert balance.pending == Decimal("0")

    def test_clear_pending_updates_timestamp(self, db_session, agent):
        repo = BalanceRepository(db_session)
        repo.add_pending(agent.id, Decimal("1.0"))
        balance = repo.clear_pending_on_confirm(agent.id, Decimal("1.0"))
        assert balance.updated_at is not None


class TestBalanceCheckConstraints:
    """Task 4: CHECK constraints prevent negative balances at DB level."""

    def test_negative_available_rejected(self, db_session, agent):
        repo = BalanceRepository(db_session)
        balance = repo.get_or_create(agent.id)
        balance.available = Decimal("-1")
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_negative_pending_rejected(self, db_session, agent):
        repo = BalanceRepository(db_session)
        balance = repo.get_or_create(agent.id)
        balance.pending = Decimal("-1")
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_zero_balances_allowed(self, db_session, agent):
        repo = BalanceRepository(db_session)
        balance = repo.get_or_create(agent.id)
        balance.available = Decimal("0")
        balance.pending = Decimal("0")
        db_session.flush()  # Should not raise


class TestPendingWithdrawalUUID:
    """Task 5: PendingWithdrawal.id and .agent_id should use UUID, not String(36)."""

    def test_id_column_is_uuid(self):
        col = PendingWithdrawal.__table__.columns["id"]
        col_type = type(col.type)
        assert col_type is not String, "PendingWithdrawal.id should be UUID, not String"

    def test_agent_id_column_is_uuid(self):
        col = PendingWithdrawal.__table__.columns["agent_id"]
        col_type = type(col.type)
        assert col_type is not String, "PendingWithdrawal.agent_id should be UUID, not String"
