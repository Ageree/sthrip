"""Tests for SpendingPolicy repository and service layers."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import Base, Agent, SpendingPolicy
from sthrip.db.spending_policy_repo import SpendingPolicyRepository
from sthrip.services.spending_policy_service import (
    SpendingPolicyService,
    PolicyViolation,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sp_engine():
    """In-memory SQLite engine with Agent + SpendingPolicy tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[Agent.__table__, SpendingPolicy.__table__],
    )
    return engine


@pytest.fixture
def sp_session(sp_engine):
    """Database session bound to the test engine."""
    factory = sessionmaker(bind=sp_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture
def test_agent(sp_session) -> Agent:
    """A minimal Agent row for FK references."""
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=f"test-agent-{uuid.uuid4().hex[:8]}",
        api_key_hash="fakehash",
    )
    sp_session.add(agent)
    sp_session.flush()
    return agent


# ---------------------------------------------------------------------------
# Repository tests
# ---------------------------------------------------------------------------

class TestSpendingPolicyRepository:
    """SpendingPolicyRepository CRUD operations."""

    def test_upsert_creates_new_policy(self, sp_session, test_agent):
        repo = SpendingPolicyRepository(sp_session)
        policy = repo.upsert(
            test_agent.id,
            max_per_tx=Decimal("1.5"),
            daily_limit=Decimal("10"),
        )

        assert policy.agent_id == test_agent.id
        assert policy.max_per_tx == Decimal("1.5")
        assert policy.daily_limit == Decimal("10")
        assert policy.max_per_session is None
        assert policy.allowed_agents is None
        assert policy.is_active is True

    def test_upsert_updates_existing_policy(self, sp_session, test_agent):
        repo = SpendingPolicyRepository(sp_session)

        repo.upsert(test_agent.id, max_per_tx=Decimal("1.0"))
        updated = repo.upsert(
            test_agent.id,
            max_per_tx=Decimal("5.0"),
            daily_limit=Decimal("20"),
        )

        assert updated.max_per_tx == Decimal("5.0")
        assert updated.daily_limit == Decimal("20")

        # Ensure only one row exists
        count = (
            sp_session.query(SpendingPolicy)
            .filter(SpendingPolicy.agent_id == test_agent.id)
            .count()
        )
        assert count == 1

    def test_get_by_agent_id_returns_policy(self, sp_session, test_agent):
        repo = SpendingPolicyRepository(sp_session)
        repo.upsert(test_agent.id, max_per_tx=Decimal("2.0"))

        found = repo.get_by_agent_id(test_agent.id)
        assert found is not None
        assert found.max_per_tx == Decimal("2.0")

    def test_get_by_agent_id_returns_none_when_missing(self, sp_session):
        repo = SpendingPolicyRepository(sp_session)
        found = repo.get_by_agent_id(uuid.uuid4())
        assert found is None


# ---------------------------------------------------------------------------
# Service tests (no Redis — Redis-dependent checks are skipped)
# ---------------------------------------------------------------------------

class _FakePolicy:
    """Lightweight stand-in for SpendingPolicy (avoids SQLAlchemy instrumentation)."""

    def __init__(self, **kwargs):
        defaults = dict(
            id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            max_per_tx=None,
            max_per_session=None,
            daily_limit=None,
            allowed_agents=None,
            blocked_agents=None,
            require_escrow_above=None,
            is_active=True,
        )
        defaults.update(kwargs)
        for k, v in defaults.items():
            object.__setattr__(self, k, v)


def _make_policy(**overrides) -> "_FakePolicy":
    """Build a fake policy object for service-layer tests."""
    return _FakePolicy(**overrides)


class TestSpendingPolicyService:
    """SpendingPolicyService validation chain."""

    def setup_method(self):
        self.svc = SpendingPolicyService(redis_client=None)

    # --- max_per_tx ---

    def test_max_per_tx_rejects_over_limit(self):
        policy = _make_policy(max_per_tx=Decimal("1.0"))
        with pytest.raises(PolicyViolation) as exc_info:
            self.svc.validate(policy, Decimal("1.5"), "recipient", "sess1")
        assert exc_info.value.field == "max_per_tx"

    def test_max_per_tx_passes_under_limit(self):
        policy = _make_policy(max_per_tx=Decimal("2.0"))
        self.svc.validate(policy, Decimal("1.5"), "recipient", "sess1")

    def test_max_per_tx_passes_at_limit(self):
        policy = _make_policy(max_per_tx=Decimal("1.0"))
        self.svc.validate(policy, Decimal("1.0"), "recipient", "sess1")

    # --- allowed_agents ---

    def test_allowed_agents_rejects_unmatched(self):
        policy = _make_policy(allowed_agents=["research-*", "data-*"])
        with pytest.raises(PolicyViolation) as exc_info:
            self.svc.validate(policy, Decimal("1"), "evil-bot", "sess1")
        assert exc_info.value.field == "allowed_agents"

    def test_allowed_agents_passes_glob_match(self):
        policy = _make_policy(allowed_agents=["research-*"])
        self.svc.validate(policy, Decimal("1"), "research-alpha", "sess1")

    def test_allowed_agents_passes_when_empty(self):
        policy = _make_policy(allowed_agents=None)
        self.svc.validate(policy, Decimal("1"), "any-agent", "sess1")

    # --- blocked_agents ---

    def test_blocked_agents_rejects_match(self):
        policy = _make_policy(blocked_agents=["scam-*"])
        with pytest.raises(PolicyViolation) as exc_info:
            self.svc.validate(policy, Decimal("1"), "scam-bot", "sess1")
        assert exc_info.value.field == "blocked_agents"

    def test_blocked_agents_passes_no_match(self):
        policy = _make_policy(blocked_agents=["scam-*"])
        self.svc.validate(policy, Decimal("1"), "research-alpha", "sess1")

    # --- require_escrow_above ---

    def test_require_escrow_above_rejects_non_escrow(self):
        policy = _make_policy(require_escrow_above=Decimal("5.0"))
        with pytest.raises(PolicyViolation) as exc_info:
            self.svc.validate(policy, Decimal("10"), "recipient", "sess1", is_escrow=False)
        assert exc_info.value.field == "require_escrow_above"

    def test_require_escrow_above_passes_when_escrow(self):
        policy = _make_policy(require_escrow_above=Decimal("5.0"))
        self.svc.validate(policy, Decimal("10"), "recipient", "sess1", is_escrow=True)

    def test_require_escrow_above_passes_under_threshold(self):
        policy = _make_policy(require_escrow_above=Decimal("5.0"))
        self.svc.validate(policy, Decimal("3"), "recipient", "sess1", is_escrow=False)

    # --- inactive policy ---

    def test_inactive_policy_skips_all_checks(self):
        policy = _make_policy(
            max_per_tx=Decimal("0.001"),
            is_active=False,
        )
        # Amount exceeds max_per_tx but policy is inactive — should pass
        self.svc.validate(policy, Decimal("999"), "recipient", "sess1")

    # --- combined policies ---

    def test_combined_policy_first_violation_wins(self):
        policy = _make_policy(
            max_per_tx=Decimal("0.5"),
            blocked_agents=["recipient"],
        )
        with pytest.raises(PolicyViolation) as exc_info:
            self.svc.validate(policy, Decimal("1"), "recipient", "sess1")
        # max_per_tx is checked before blocked_agents
        assert exc_info.value.field == "max_per_tx"
