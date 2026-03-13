"""Test that get_db_readonly actually prevents writes."""
import pytest
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import Base, Agent


@pytest.fixture
def _readonly_engine():
    """Standalone engine for readonly tests (no conftest db_engine dependency)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Agent.__table__])
    return engine


@pytest.fixture
def _readonly_session_factory(_readonly_engine):
    return sessionmaker(bind=_readonly_engine, expire_on_commit=False)


def test_readonly_session_rejects_flush(_readonly_session_factory):
    """A readonly session must raise on flush (attempted write)."""
    import sthrip.db.database as db_mod

    original_factory = db_mod._SessionFactory
    db_mod._SessionFactory = _readonly_session_factory
    try:
        with db_mod.get_db_readonly() as db:
            agent = Agent(agent_name="test_readonly", xmr_address="5" + "A" * 94)
            db.add(agent)
            with pytest.raises(RuntimeError, match="readonly"):
                db.flush()
    finally:
        db_mod._SessionFactory = original_factory


def test_readonly_session_rejects_commit(_readonly_session_factory):
    """A readonly session must also raise on explicit commit."""
    import sthrip.db.database as db_mod

    original_factory = db_mod._SessionFactory
    db_mod._SessionFactory = _readonly_session_factory
    try:
        with db_mod.get_db_readonly() as db:
            agent = Agent(agent_name="test_readonly2", xmr_address="5" + "B" * 94)
            db.add(agent)
            with pytest.raises(RuntimeError, match="readonly"):
                db.commit()
    finally:
        db_mod._SessionFactory = original_factory


def test_regular_session_allows_writes(_readonly_session_factory):
    """A regular session must allow writes normally."""
    import sthrip.db.database as db_mod

    original_factory = db_mod._SessionFactory
    db_mod._SessionFactory = _readonly_session_factory
    try:
        with db_mod.get_db() as db:
            agent = Agent(agent_name="test_write", xmr_address="5" + "C" * 94)
            db.add(agent)
            db.flush()  # Should NOT raise
            assert agent.id is not None
    finally:
        db_mod._SessionFactory = original_factory
