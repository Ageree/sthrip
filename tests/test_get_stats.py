"""Tests for optimized get_stats."""

import pytest
from uuid import uuid4
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sthrip.db.models import (
    Base, Agent, AgentReputation,
    AgentTier, RateLimitTier, PrivacyLevel,
)

_TEST_TABLES = [Agent.__table__, AgentReputation.__table__]


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _create_agent(session, name, tier=AgentTier.FREE):
    agent = Agent(
        id=uuid4(), agent_name=name, is_active=True,
        tier=tier, rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.MEDIUM,
    )
    session.add(agent)
    session.flush()
    return agent


def test_get_stats_returns_correct_structure(db_session):
    from sthrip.services.agent_registry import AgentRegistry

    _create_agent(db_session, "a1", AgentTier.FREE)
    _create_agent(db_session, "a2", AgentTier.VERIFIED)
    _create_agent(db_session, "a3", AgentTier.FREE)
    db_session.commit()

    from contextlib import contextmanager

    @contextmanager
    def mock_get_db():
        yield db_session

    registry = AgentRegistry()
    with patch("sthrip.services.agent_registry.get_db", mock_get_db):
        stats = registry.get_stats()

    assert stats["total_agents"] == 3
    assert "by_tier" in stats
    assert stats["by_tier"]["free"] == 2
    assert stats["by_tier"]["verified"] == 1
    assert "verified_count" in stats
    assert "active_last_24h" in stats
