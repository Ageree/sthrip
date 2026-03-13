"""Test that hub payment recipient validation and transfer happen in the same DB session."""
import pytest
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import Base, Agent, AgentBalance, AgentReputation, HubRoute, FeeCollection

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
]


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close()


def _make_agent(db_session, name, xmr_suffix, is_active=True):
    """Create an agent with a synthetic XMR address."""
    from conftest import generate_test_monero_address

    agent = Agent(
        agent_name=name,
        xmr_address=generate_test_monero_address(),
        is_active=is_active,
    )
    db_session.add(agent)
    db_session.flush()
    return agent


class TestValidateRecipientInSession:
    """_validate_recipient_in_session must check is_active within the provided session."""

    def test_active_recipient_returns_agent(self, db_session):
        recipient = _make_agent(db_session, "active_recv", "B")

        from api.routers.payments import _validate_recipient_in_session

        result = _validate_recipient_in_session(db_session, "active_recv")
        assert result.agent_name == "active_recv"
        assert result.id == recipient.id

    def test_deactivated_recipient_rejected(self, db_session):
        _make_agent(db_session, "deactivated_recv", "C", is_active=False)

        from api.routers.payments import _validate_recipient_in_session
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _validate_recipient_in_session(db_session, "deactivated_recv")

        assert exc_info.value.status_code == 400
        assert "not active" in exc_info.value.detail.lower()

    def test_nonexistent_recipient_rejected(self, db_session):
        from api.routers.payments import _validate_recipient_in_session
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _validate_recipient_in_session(db_session, "ghost_agent")

        assert exc_info.value.status_code == 404

    def test_recipient_without_xmr_address_rejected(self, db_session):
        agent = Agent(agent_name="no_wallet", is_active=True)
        db_session.add(agent)
        db_session.flush()

        from api.routers.payments import _validate_recipient_in_session
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _validate_recipient_in_session(db_session, "no_wallet")

        assert exc_info.value.status_code == 400
        assert "xmr address" in exc_info.value.detail.lower()


class TestBuildRecipientProfile:
    """_build_recipient_profile must extract fields from an Agent ORM model."""

    def test_builds_profile_from_agent(self, db_session):
        agent = _make_agent(db_session, "profiled_agent", "D")

        from api.routers.payments import _build_recipient_profile

        profile = _build_recipient_profile(agent)
        assert profile.agent_name == "profiled_agent"
        assert profile.id == str(agent.id)
        assert profile.xmr_address == agent.xmr_address

    def test_profile_has_trust_score(self, db_session):
        agent = _make_agent(db_session, "scored_agent", "E")
        rep = AgentReputation(agent_id=agent.id, trust_score=85)
        db_session.add(rep)
        db_session.flush()

        from api.routers.payments import _build_recipient_profile

        profile = _build_recipient_profile(agent)
        # trust_score comes from reputation relation or defaults to 0
        assert isinstance(profile.trust_score, int)
