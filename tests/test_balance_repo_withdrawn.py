"""Test that withdrawal operations update total_withdrawn."""
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from sthrip.db.models import Base, Agent, AgentBalance
from sthrip.db.balance_repo import BalanceRepository


@pytest.fixture
def bal_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[
        Agent.__table__,
        AgentBalance.__table__,
    ])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close()


def test_withdraw_updates_total_withdrawn(bal_session):
    db = bal_session
    agent_id = uuid4()

    # Create agent and seed balance
    agent = Agent(id=agent_id, agent_name="test-agent")
    db.add(agent)
    db.flush()

    repo = BalanceRepository(db)
    repo.deposit(agent_id, Decimal("10.0"))
    db.flush()

    # Withdraw
    repo.withdraw(agent_id, Decimal("3.0"))
    db.flush()

    balance = repo.get_or_create(agent_id, "XMR")
    assert balance.available == Decimal("7.0")
    assert balance.total_withdrawn == Decimal("3.0")


def test_withdraw_insufficient_balance_raises(bal_session):
    db = bal_session
    agent_id = uuid4()

    agent = Agent(id=agent_id, agent_name="test-agent-2")
    db.add(agent)
    db.flush()

    repo = BalanceRepository(db)
    repo.deposit(agent_id, Decimal("1.0"))
    db.flush()

    with pytest.raises(ValueError, match="Insufficient balance"):
        repo.withdraw(agent_id, Decimal("5.0"))
