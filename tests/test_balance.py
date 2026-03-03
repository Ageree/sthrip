"""Tests for balance repository"""
import pytest
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import create_engine, String, event
from sqlalchemy.orm import sessionmaker
from stealthpay.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    AgentTier, RateLimitTier, PrivacyLevel,
)
from stealthpay.db.repository import BalanceRepository

# Only create the tables we need for testing (avoids INET type issue with SQLite)
_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
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
