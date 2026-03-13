"""Test that withdrawing to own deposit address is rejected before balance deduction."""
import pytest
from contextlib import contextmanager
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, PendingWithdrawal,
)
from sthrip.db.repository import BalanceRepository

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


@contextmanager
def _fake_get_db(session):
    """Return a context manager that yields the given session (no commit/rollback)."""
    yield session


def test_self_send_rejected_before_deduction(db_session):
    """Withdrawing to own deposit address must fail with 400 WITHOUT deducting balance."""
    agent_id = uuid4()
    deposit_addr = "5" + "B" * 94

    repo = BalanceRepository(db_session)
    balance = repo.get_or_create(agent_id)
    balance.deposit_address = deposit_addr
    balance.available = Decimal("10.0")
    db_session.flush()

    from api.routers.balance import _deduct_and_create_pending
    from fastapi import HTTPException

    with patch("api.routers.balance.get_db", side_effect=lambda: _fake_get_db(db_session)):
        with pytest.raises(HTTPException) as exc_info:
            _deduct_and_create_pending(agent_id, Decimal("1.0"), deposit_addr, check_self_send=True)

    assert exc_info.value.status_code == 400
    assert "own deposit" in exc_info.value.detail.lower() or "self" in exc_info.value.detail.lower()

    # Balance must NOT be deducted
    db_session.refresh(balance)
    assert balance.available == Decimal("10.0")


def test_different_address_passes_self_send_check(db_session):
    """Withdrawing to a different address should pass validation."""
    agent_id = uuid4()
    deposit_addr = "5" + "B" * 94
    other_addr = "5" + "C" * 94

    repo = BalanceRepository(db_session)
    balance = repo.get_or_create(agent_id)
    balance.deposit_address = deposit_addr
    balance.available = Decimal("10.0")
    db_session.flush()

    from api.routers.balance import _deduct_and_create_pending

    with patch("api.routers.balance.get_db", side_effect=lambda: _fake_get_db(db_session)):
        # Should NOT raise — different address
        pending_id = _deduct_and_create_pending(agent_id, Decimal("1.0"), other_addr, check_self_send=True)
        assert pending_id is not None


def test_no_deposit_address_passes_check(db_session):
    """If agent has no deposit address yet, any withdrawal address is fine."""
    agent_id = uuid4()
    repo = BalanceRepository(db_session)
    balance = repo.get_or_create(agent_id)
    balance.available = Decimal("10.0")
    # No deposit_address set
    db_session.flush()

    from api.routers.balance import _deduct_and_create_pending

    with patch("api.routers.balance.get_db", side_effect=lambda: _fake_get_db(db_session)):
        pending_id = _deduct_and_create_pending(agent_id, Decimal("1.0"), "5" + "D" * 94, check_self_send=True)
        assert pending_id is not None
