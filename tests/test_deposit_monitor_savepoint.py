"""Test that duplicate tx_hash in a batch does NOT roll back prior transfers."""
import pytest
from unittest.mock import MagicMock
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError

from sthrip.db.models import Base, Agent, AgentBalance, Transaction
from sthrip.db.transaction_repo import TransactionRepository
from sthrip.db.balance_repo import BalanceRepository


@pytest.fixture
def savepoint_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[
        Agent.__table__,
        AgentBalance.__table__,
        Transaction.__table__,
    ])
    return engine


@pytest.fixture
def savepoint_session(savepoint_engine):
    Session = sessionmaker(bind=savepoint_engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close()


def test_duplicate_tx_does_not_rollback_prior_transfers(savepoint_session):
    """When transfer #2 in a batch is a duplicate, transfer #1 stays committed."""
    from sthrip.services.deposit_monitor import DepositMonitor

    db = savepoint_session
    agent_id = uuid4()

    # Create agent and balance records
    agent = Agent(id=agent_id, agent_name="test-agent", api_key_hash="fakehash")
    db.add(agent)
    balance = AgentBalance(agent_id=agent_id, token="XMR")
    db.add(balance)
    db.flush()

    tx_repo = TransactionRepository(db)
    bal_repo = BalanceRepository(db)

    monitor = DepositMonitor.__new__(DepositMonitor)
    monitor.min_confirmations = 10
    monitor._network = "stagenet"
    monitor._fire_webhook = MagicMock()

    # Transfer 1: normal (should persist)
    monitor._handle_new_transfer(
        db, tx_repo, bal_repo, agent_id,
        txid="aaa111", amount=Decimal("1.0"), confirmations=10, height=100,
    )
    db.flush()

    # Transfer 2: make tx_repo.create raise IntegrityError (duplicate)
    original_create = tx_repo.create

    def create_that_fails_on_duplicate(**kwargs):
        if kwargs.get("tx_hash") == "bbb222":
            raise IntegrityError("duplicate", {}, None)
        return original_create(**kwargs)

    tx_repo.create = create_that_fails_on_duplicate

    # Should NOT raise, should skip the duplicate
    monitor._handle_new_transfer(
        db, tx_repo, bal_repo, agent_id,
        txid="bbb222", amount=Decimal("2.0"), confirmations=10, height=101,
    )

    # Transfer 1 balance must still be there
    bal = bal_repo.get_available(agent_id, "XMR")
    assert bal >= Decimal("1.0"), f"Transfer 1 was rolled back! balance={bal}"
