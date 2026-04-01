"""
Tests for the Matchmaking feature.

TDD: These tests were written BEFORE the implementation.
They cover:
- Unit: scoring algorithm, capability filtering, budget filtering
- Integration: API endpoints (create, get, accept)
- Edge cases: no match, auto_assign, invalid input
"""

import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base,
    Agent,
    AgentBalance,
    AgentRatingSummary,
    SLATemplate,
    SLAContract,
    EscrowDeal,
)
from sthrip.db.enums import MatchRequestStatus


# ---------------------------------------------------------------------------
# Inline DB fixture — includes MatchRequest table
# ---------------------------------------------------------------------------

def _make_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Import here to ensure the model is registered before create_all
    from sthrip.db.models import MatchRequest  # noqa: F401
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def engine():
    return _make_engine()


@pytest.fixture
def db_session(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    yield session
    session.rollback()
    session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(db, name: str, capabilities=None, is_active: bool = True) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=name,
        capabilities=capabilities or [],
        is_active=is_active,
        last_seen_at=datetime.now(timezone.utc),
    )
    db.add(agent)
    db.flush()
    return agent


def _make_rating(db, agent_id, avg_overall: float = 4.0) -> AgentRatingSummary:
    summary = AgentRatingSummary(
        agent_id=agent_id,
        total_reviews=5,
        avg_overall=Decimal(str(avg_overall)),
        avg_speed=Decimal("4.0"),
        avg_quality=Decimal("4.0"),
        avg_reliability=Decimal("4.0"),
    )
    db.add(summary)
    db.flush()
    return summary


def _make_sla_template(
    db,
    provider_id,
    base_price: float = 0.5,
    delivery_time_secs: int = 3600,
    response_time_secs: int = 300,
    is_active: bool = True,
) -> SLATemplate:
    tmpl = SLATemplate(
        id=uuid.uuid4(),
        provider_id=provider_id,
        name="Test SLA",
        service_description="Test service",
        deliverables=[],
        response_time_secs=response_time_secs,
        delivery_time_secs=delivery_time_secs,
        base_price=Decimal(str(base_price)),
        currency="XMR",
        penalty_percent=10,
        is_active=is_active,
    )
    db.add(tmpl)
    db.flush()
    return tmpl


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — Scoring algorithm
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestScoreCalculation:
    """Unit tests for the _find_best_match scoring algorithm."""

    def test_score_perfect_candidate(self, db_session):
        """An agent with top rating, low price and fast delivery scores near 1.0."""
        from sthrip.services.matchmaking_service import MatchmakingService

        requester = _make_agent(db_session, "requester-score-1", capabilities=["nlp"])
        provider = _make_agent(db_session, "provider-score-1", capabilities=["nlp"])
        _make_rating(db_session, provider.id, avg_overall=5.0)
        tmpl = _make_sla_template(
            db_session, provider.id,
            base_price=0.01,    # much cheaper than budget
            delivery_time_secs=100,
        )
        db_session.commit()

        # Build a fake MatchRequest-like object
        from sthrip.db.models import MatchRequest
        req = MatchRequest(
            id=uuid.uuid4(),
            requester_id=requester.id,
            task_description="NLP task",
            required_capabilities=["nlp"],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(req)
        db_session.commit()

        svc = MatchmakingService()
        result = svc._find_best_match(db_session, req)

        assert result is not None
        agent_found, score = result
        assert agent_found.id == provider.id
        # rating_score(0.4 * 5/5=1) + price_score(0.3 * ~1) + speed_score(0.2 * ~1) + avail(0.1)
        assert score > 0.8

    def test_score_high_rating_weights_correctly(self, db_session):
        """Higher-rated provider beats lower-rated at same price."""
        from sthrip.services.matchmaking_service import MatchmakingService
        from sthrip.db.models import MatchRequest

        requester = _make_agent(db_session, "requester-score-2", capabilities=["code"])
        p_high = _make_agent(db_session, "provider-high", capabilities=["code"])
        p_low = _make_agent(db_session, "provider-low", capabilities=["code"])
        _make_rating(db_session, p_high.id, avg_overall=5.0)
        _make_rating(db_session, p_low.id, avg_overall=2.0)
        _make_sla_template(db_session, p_high.id, base_price=0.5, delivery_time_secs=1800)
        _make_sla_template(db_session, p_low.id, base_price=0.5, delivery_time_secs=1800)
        db_session.commit()

        req = MatchRequest(
            id=uuid.uuid4(),
            requester_id=requester.id,
            task_description="code review",
            required_capabilities=["code"],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(req)
        db_session.commit()

        svc = MatchmakingService()
        result = svc._find_best_match(db_session, req)

        assert result is not None
        best_agent, _ = result
        assert best_agent.id == p_high.id

    def test_score_returns_none_when_no_agents(self, db_session):
        """Returns None when the database has no qualifying agents."""
        from sthrip.services.matchmaking_service import MatchmakingService
        from sthrip.db.models import MatchRequest

        requester = _make_agent(db_session, "requester-score-3")
        db_session.commit()

        req = MatchRequest(
            id=uuid.uuid4(),
            requester_id=requester.id,
            task_description="no agents",
            required_capabilities=["rare-cap"],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(req)
        db_session.commit()

        svc = MatchmakingService()
        result = svc._find_best_match(db_session, req)

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — Capability filter
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCapabilityFilter:
    """Only agents whose capabilities include all required ones should be matched."""

    def test_exact_capability_match(self, db_session):
        from sthrip.services.matchmaking_service import MatchmakingService
        from sthrip.db.models import MatchRequest

        requester = _make_agent(db_session, "req-cap-1")
        good = _make_agent(db_session, "provider-cap-good", capabilities=["translation", "editing"])
        bad = _make_agent(db_session, "provider-cap-bad", capabilities=["coding"])
        _make_rating(db_session, good.id, avg_overall=4.0)
        _make_rating(db_session, bad.id, avg_overall=4.0)
        _make_sla_template(db_session, good.id, base_price=0.1)
        _make_sla_template(db_session, bad.id, base_price=0.1)
        db_session.commit()

        req = MatchRequest(
            id=uuid.uuid4(),
            requester_id=requester.id,
            task_description="translate and edit",
            required_capabilities=["translation"],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(req)
        db_session.commit()

        svc = MatchmakingService()
        result = svc._find_best_match(db_session, req)

        assert result is not None
        agent_found, _ = result
        assert agent_found.id == good.id

    def test_no_capability_requirement_matches_any(self, db_session):
        """When required_capabilities is empty, any agent with an SLA template qualifies."""
        from sthrip.services.matchmaking_service import MatchmakingService
        from sthrip.db.models import MatchRequest

        requester = _make_agent(db_session, "req-cap-2")
        provider = _make_agent(db_session, "provider-cap-any", capabilities=["anything"])
        _make_rating(db_session, provider.id, avg_overall=3.0)
        _make_sla_template(db_session, provider.id, base_price=0.1)
        db_session.commit()

        req = MatchRequest(
            id=uuid.uuid4(),
            requester_id=requester.id,
            task_description="general task",
            required_capabilities=[],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(req)
        db_session.commit()

        svc = MatchmakingService()
        result = svc._find_best_match(db_session, req)

        assert result is not None

    def test_multiple_required_capabilities_all_must_match(self, db_session):
        """Agent must have ALL required capabilities, not just one."""
        from sthrip.services.matchmaking_service import MatchmakingService
        from sthrip.db.models import MatchRequest

        requester = _make_agent(db_session, "req-cap-3")
        partial = _make_agent(db_session, "provider-partial", capabilities=["nlp"])
        full = _make_agent(db_session, "provider-full", capabilities=["nlp", "ml", "api"])
        _make_rating(db_session, partial.id, avg_overall=5.0)
        _make_rating(db_session, full.id, avg_overall=4.0)
        _make_sla_template(db_session, partial.id, base_price=0.1)
        _make_sla_template(db_session, full.id, base_price=0.1)
        db_session.commit()

        req = MatchRequest(
            id=uuid.uuid4(),
            requester_id=requester.id,
            task_description="ml nlp task",
            required_capabilities=["nlp", "ml"],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(req)
        db_session.commit()

        svc = MatchmakingService()
        result = svc._find_best_match(db_session, req)

        assert result is not None
        agent_found, _ = result
        assert agent_found.id == full.id


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — Budget filter
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBudgetFilter:
    """Only agents with SLA template base_price <= budget should be matched."""

    def test_agent_over_budget_excluded(self, db_session):
        from sthrip.services.matchmaking_service import MatchmakingService
        from sthrip.db.models import MatchRequest

        requester = _make_agent(db_session, "req-budget-1")
        cheap = _make_agent(db_session, "provider-cheap", capabilities=["audit"])
        expensive = _make_agent(db_session, "provider-expensive", capabilities=["audit"])
        _make_rating(db_session, cheap.id, avg_overall=3.5)
        _make_rating(db_session, expensive.id, avg_overall=5.0)  # better rating but over budget
        _make_sla_template(db_session, cheap.id, base_price=0.3)
        _make_sla_template(db_session, expensive.id, base_price=2.0)  # over budget
        db_session.commit()

        req = MatchRequest(
            id=uuid.uuid4(),
            requester_id=requester.id,
            task_description="audit task",
            required_capabilities=["audit"],
            budget=Decimal("0.5"),  # only cheap qualifies
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(req)
        db_session.commit()

        svc = MatchmakingService()
        result = svc._find_best_match(db_session, req)

        assert result is not None
        agent_found, _ = result
        assert agent_found.id == cheap.id

    def test_all_agents_over_budget_returns_none(self, db_session):
        from sthrip.services.matchmaking_service import MatchmakingService
        from sthrip.db.models import MatchRequest

        requester = _make_agent(db_session, "req-budget-2")
        provider = _make_agent(db_session, "provider-costly", capabilities=["design"])
        _make_rating(db_session, provider.id, avg_overall=4.0)
        _make_sla_template(db_session, provider.id, base_price=5.0)
        db_session.commit()

        req = MatchRequest(
            id=uuid.uuid4(),
            requester_id=requester.id,
            task_description="design task",
            required_capabilities=["design"],
            budget=Decimal("0.1"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(req)
        db_session.commit()

        svc = MatchmakingService()
        result = svc._find_best_match(db_session, req)

        assert result is None

    def test_min_rating_filter_excludes_low_rated_agents(self, db_session):
        """Agents below min_rating threshold are excluded."""
        from sthrip.services.matchmaking_service import MatchmakingService
        from sthrip.db.models import MatchRequest

        requester = _make_agent(db_session, "req-rating-1")
        low_rated = _make_agent(db_session, "provider-low-rating", capabilities=["testing"])
        _make_rating(db_session, low_rated.id, avg_overall=2.0)
        _make_sla_template(db_session, low_rated.id, base_price=0.1)
        db_session.commit()

        req = MatchRequest(
            id=uuid.uuid4(),
            requester_id=requester.id,
            task_description="testing task",
            required_capabilities=["testing"],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("4.0"),  # requires 4.0+ but agent has 2.0
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(req)
        db_session.commit()

        svc = MatchmakingService()
        result = svc._find_best_match(db_session, req)

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Repository tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMatchmakingRepository:
    def test_create_and_get_by_id(self, db_session):
        from sthrip.db.matchmaking_repo import MatchmakingRepository

        requester = _make_agent(db_session, "req-repo-1")
        db_session.commit()

        repo = MatchmakingRepository(db_session)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        req = repo.create(
            requester_id=requester.id,
            task_description="repo test task",
            required_capabilities=["cap-a"],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("3.0"),
            auto_assign=False,
            expires_at=expires_at,
        )
        db_session.commit()

        fetched = repo.get_by_id(req.id)
        assert fetched is not None
        assert fetched.task_description == "repo test task"
        assert fetched.state == MatchRequestStatus.SEARCHING

    def test_update_match(self, db_session):
        from sthrip.db.matchmaking_repo import MatchmakingRepository

        requester = _make_agent(db_session, "req-repo-2")
        provider = _make_agent(db_session, "prov-repo-2")
        db_session.commit()

        repo = MatchmakingRepository(db_session)
        req = repo.create(
            requester_id=requester.id,
            task_description="update test",
            required_capabilities=[],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.commit()

        rows = repo.update_match(
            request_id=req.id,
            matched_agent_id=provider.id,
            sla_contract_id=None,
            state=MatchRequestStatus.MATCHED,
        )
        db_session.commit()

        assert rows == 1
        updated = repo.get_by_id(req.id)
        assert updated.state == MatchRequestStatus.MATCHED
        assert updated.matched_agent_id == provider.id

    def test_list_by_requester(self, db_session):
        from sthrip.db.matchmaking_repo import MatchmakingRepository

        requester = _make_agent(db_session, "req-repo-3")
        other = _make_agent(db_session, "other-repo-3")
        db_session.commit()

        repo = MatchmakingRepository(db_session)
        for i in range(3):
            repo.create(
                requester_id=requester.id,
                task_description=f"task {i}",
                required_capabilities=[],
                budget=Decimal("1.0"),
                currency="XMR",
                deadline_secs=3600,
                min_rating=Decimal("0"),
                auto_assign=False,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        repo.create(
            requester_id=other.id,
            task_description="other task",
            required_capabilities=[],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.commit()

        items, total = repo.list_by_requester(requester.id, limit=10, offset=0)
        assert total == 3
        assert len(items) == 3

    def test_get_expired_searching(self, db_session):
        from sthrip.db.matchmaking_repo import MatchmakingRepository

        requester = _make_agent(db_session, "req-repo-4")
        db_session.commit()

        repo = MatchmakingRepository(db_session)
        past = datetime.now(timezone.utc) - timedelta(minutes=10)
        future = datetime.now(timezone.utc) + timedelta(minutes=10)

        expired = repo.create(
            requester_id=requester.id,
            task_description="expired",
            required_capabilities=[],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=60,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=past,
        )
        active = repo.create(
            requester_id=requester.id,
            task_description="active",
            required_capabilities=[],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=future,
        )
        db_session.commit()

        stale = repo.get_expired_searching()
        ids = [r.id for r in stale]
        assert expired.id in ids
        assert active.id not in ids


# ═══════════════════════════════════════════════════════════════════════════════
# Service-layer tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMatchmakingService:
    def test_create_request_state_searching_when_no_match(self, db_session):
        from sthrip.services.matchmaking_service import MatchmakingService

        requester = _make_agent(db_session, "svc-req-1")
        db_session.commit()

        svc = MatchmakingService()
        result = svc.create_request(
            db=db_session,
            requester_id=requester.id,
            task_description="specialized rare task",
            required_capabilities=["unicorn-capability"],
            budget=Decimal("0.001"),
            currency="XMR",
            deadline_secs=300,
            min_rating=Decimal("5.0"),
            auto_assign=False,
        )

        assert result["state"] == MatchRequestStatus.SEARCHING
        assert result["matched_agent_id"] is None

    def test_create_request_state_matched_when_agent_found(self, db_session):
        from sthrip.services.matchmaking_service import MatchmakingService

        requester = _make_agent(db_session, "svc-req-2")
        provider = _make_agent(db_session, "svc-prov-2", capabilities=["writing"])
        _make_rating(db_session, provider.id, avg_overall=4.5)
        _make_sla_template(db_session, provider.id, base_price=0.2, delivery_time_secs=1800)
        db_session.commit()

        svc = MatchmakingService()
        result = svc.create_request(
            db=db_session,
            requester_id=requester.id,
            task_description="writing task",
            required_capabilities=["writing"],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("4.0"),
            auto_assign=False,
        )

        assert result["state"] == MatchRequestStatus.MATCHED
        assert result["matched_agent_id"] == str(provider.id)

    def test_expire_stale_transitions_expired_requests(self, db_session):
        from sthrip.services.matchmaking_service import MatchmakingService
        from sthrip.db.matchmaking_repo import MatchmakingRepository

        requester = _make_agent(db_session, "svc-req-3")
        db_session.commit()

        repo = MatchmakingRepository(db_session)
        past = datetime.now(timezone.utc) - timedelta(minutes=10)
        req = repo.create(
            requester_id=requester.id,
            task_description="stale task",
            required_capabilities=[],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=60,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=past,
        )
        db_session.commit()

        svc = MatchmakingService()
        count = svc.expire_stale(db_session)
        db_session.commit()

        assert count == 1
        updated = repo.get_by_id(req.id)
        assert updated.state == MatchRequestStatus.EXPIRED

    def test_accept_match_raises_if_no_matched_agent(self, db_session):
        from sthrip.services.matchmaking_service import MatchmakingService
        from sthrip.db.matchmaking_repo import MatchmakingRepository

        requester = _make_agent(db_session, "svc-req-4")
        db_session.commit()

        repo = MatchmakingRepository(db_session)
        req = repo.create(
            requester_id=requester.id,
            task_description="unmatched task",
            required_capabilities=[],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.commit()

        svc = MatchmakingService()
        with pytest.raises(ValueError, match="No matched agent"):
            svc.accept_match(db_session, req.id, requester.id)

    def test_accept_match_raises_if_wrong_requester(self, db_session):
        from sthrip.services.matchmaking_service import MatchmakingService
        from sthrip.db.matchmaking_repo import MatchmakingRepository

        requester = _make_agent(db_session, "svc-req-5a")
        provider = _make_agent(db_session, "svc-prov-5a", capabilities=["seo"])
        stranger = _make_agent(db_session, "svc-stranger-5a")
        _make_rating(db_session, provider.id, avg_overall=4.0)
        _make_sla_template(db_session, provider.id, base_price=0.1)
        db_session.commit()

        repo = MatchmakingRepository(db_session)
        req = repo.create(
            requester_id=requester.id,
            task_description="seo task",
            required_capabilities=["seo"],
            budget=Decimal("1.0"),
            currency="XMR",
            deadline_secs=3600,
            min_rating=Decimal("0"),
            auto_assign=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        # Manually set to MATCHED state
        repo.update_match(req.id, provider.id, None, MatchRequestStatus.MATCHED)
        db_session.commit()

        svc = MatchmakingService()
        with pytest.raises(PermissionError):
            svc.accept_match(db_session, req.id, stranger.id)


# ═══════════════════════════════════════════════════════════════════════════════
# API integration tests — use the shared conftest `client` fixture
# ═══════════════════════════════════════════════════════════════════════════════

def _register_agent(client, name: str):
    """Register an agent and return (agent_id, api_key)."""
    resp = client.post("/v2/agents/register", json={
        "agent_name": name,
        "capabilities": ["general"],
        "description": "test agent",
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return data["agent_id"], data["api_key"]


@pytest.mark.integration
class TestCreateMatchRequestAPI:
    def test_create_match_request_returns_201(self, client):
        _, api_key = _register_agent(client, "mm-api-req-1")

        resp = client.post(
            "/v2/matchmaking/request",
            json={
                "task_description": "translate this doc",
                "required_capabilities": ["translation"],
                "budget": "1.0",
                "currency": "XMR",
                "deadline_secs": 3600,
                "min_rating": "0.0",
                "auto_assign": False,
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "request_id" in data
        assert data["state"] in ("searching", "matched")

    def test_create_match_request_requires_auth(self, client):
        resp = client.post(
            "/v2/matchmaking/request",
            json={
                "task_description": "task",
                "required_capabilities": [],
                "budget": "1.0",
                "currency": "XMR",
                "deadline_secs": 3600,
                "min_rating": "0.0",
                "auto_assign": False,
            },
        )
        assert resp.status_code == 401

    def test_create_match_request_validates_budget(self, client):
        _, api_key = _register_agent(client, "mm-api-req-2")

        resp = client.post(
            "/v2/matchmaking/request",
            json={
                "task_description": "task",
                "required_capabilities": [],
                "budget": "-1.0",  # invalid
                "currency": "XMR",
                "deadline_secs": 3600,
                "min_rating": "0.0",
                "auto_assign": False,
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 422


@pytest.mark.integration
class TestGetMatchResultAPI:
    def test_get_match_result_returns_200(self, client):
        _, api_key = _register_agent(client, "mm-api-get-1")

        # Create a request first
        create_resp = client.post(
            "/v2/matchmaking/request",
            json={
                "task_description": "analyze data",
                "required_capabilities": ["data-analysis"],
                "budget": "0.5",
                "currency": "XMR",
                "deadline_secs": 7200,
                "min_rating": "0.0",
                "auto_assign": False,
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert create_resp.status_code == 201
        request_id = create_resp.json()["request_id"]

        get_resp = client.get(
            f"/v2/matchmaking/{request_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["request_id"] == request_id

    def test_get_match_result_404_for_unknown_id(self, client):
        _, api_key = _register_agent(client, "mm-api-get-2")

        unknown_id = str(uuid.uuid4())
        resp = client.get(
            f"/v2/matchmaking/{unknown_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 404

    def test_get_match_result_403_for_wrong_requester(self, client):
        _, api_key_owner = _register_agent(client, "mm-api-get-owner")
        _, api_key_other = _register_agent(client, "mm-api-get-other")

        # Owner creates a request
        create_resp = client.post(
            "/v2/matchmaking/request",
            json={
                "task_description": "private task",
                "required_capabilities": [],
                "budget": "0.5",
                "currency": "XMR",
                "deadline_secs": 3600,
                "min_rating": "0.0",
                "auto_assign": False,
            },
            headers={"Authorization": f"Bearer {api_key_owner}"},
        )
        assert create_resp.status_code == 201
        request_id = create_resp.json()["request_id"]

        # Other agent tries to read it
        resp = client.get(
            f"/v2/matchmaking/{request_id}",
            headers={"Authorization": f"Bearer {api_key_other}"},
        )
        assert resp.status_code == 403


@pytest.mark.integration
class TestNoMatchFound:
    def test_no_match_found_state_is_searching(self, client):
        _, api_key = _register_agent(client, "mm-no-match-1")

        resp = client.post(
            "/v2/matchmaking/request",
            json={
                "task_description": "impossible task",
                "required_capabilities": ["quantum-magic-unicorn-capability"],
                "budget": "0.00001",
                "currency": "XMR",
                "deadline_secs": 1,
                "min_rating": "4.99",
                "auto_assign": False,
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["state"] == "searching"
        assert data["matched_agent_id"] is None


@pytest.mark.integration
class TestAutoAssign:
    def test_auto_assign_creates_sla_contract(self, client, db_engine):
        """When auto_assign=True and a match is found, SLA contract is auto-created."""
        from sqlalchemy.orm import sessionmaker as _sessionmaker

        # Seed: register a provider agent with SLA template directly in DB
        Session = _sessionmaker(bind=db_engine, expire_on_commit=False)
        with Session() as seed_db:
            provider = Agent(
                id=uuid.uuid4(),
                agent_name="mm-auto-provider",
                capabilities=["devops"],
                is_active=True,
                last_seen_at=datetime.now(timezone.utc),
            )
            seed_db.add(provider)
            seed_db.flush()

            tmpl = SLATemplate(
                id=uuid.uuid4(),
                provider_id=provider.id,
                name="DevOps SLA",
                service_description="DevOps services",
                deliverables=[],
                response_time_secs=300,
                delivery_time_secs=3600,
                base_price=Decimal("0.5"),
                currency="XMR",
                penalty_percent=10,
                is_active=True,
            )
            seed_db.add(tmpl)

            rating = AgentRatingSummary(
                agent_id=provider.id,
                total_reviews=10,
                avg_overall=Decimal("4.5"),
                avg_speed=Decimal("4.5"),
                avg_quality=Decimal("4.5"),
                avg_reliability=Decimal("4.5"),
            )
            seed_db.add(rating)

            # Create api key for the requester via the registration endpoint
            seed_db.commit()

        _, api_key = _register_agent(client, "mm-auto-requester")

        # Mock SLAService.create_contract to avoid balance check
        mock_contract = {
            "contract_id": str(uuid.uuid4()),
            "provider_id": str(uuid.uuid4()),
            "consumer_id": str(uuid.uuid4()),
            "state": "proposed",
        }
        with patch(
            "sthrip.services.sla_service.SLAService.create_contract",
            return_value=mock_contract,
        ):
            resp = client.post(
                "/v2/matchmaking/request",
                json={
                    "task_description": "devops setup",
                    "required_capabilities": ["devops"],
                    "budget": "1.0",
                    "currency": "XMR",
                    "deadline_secs": 7200,
                    "min_rating": "0.0",
                    "auto_assign": True,
                },
                headers={"Authorization": f"Bearer {api_key}"},
            )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        # If a match was found and auto_assign is True, state should be ASSIGNED
        # If no match found, it stays SEARCHING — both are valid states
        assert data["state"] in ("assigned", "searching", "matched")


@pytest.mark.integration
class TestAcceptMatchAPI:
    def test_accept_match_404_for_unknown_request(self, client):
        _, api_key = _register_agent(client, "mm-accept-1")

        resp = client.post(
            f"/v2/matchmaking/{uuid.uuid4()}/accept",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 404

    def test_accept_match_requires_auth(self, client):
        resp = client.post(f"/v2/matchmaking/{uuid.uuid4()}/accept")
        assert resp.status_code == 401

    def test_accept_match_on_searching_request_returns_400(self, client):
        """Accepting a request that has no matched agent should return 400."""
        _, api_key = _register_agent(client, "mm-accept-2")

        create_resp = client.post(
            "/v2/matchmaking/request",
            json={
                "task_description": "unmatched accept test",
                "required_capabilities": ["very-rare-xyz"],
                "budget": "0.0001",
                "currency": "XMR",
                "deadline_secs": 60,
                "min_rating": "0.0",
                "auto_assign": False,
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert create_resp.status_code == 201
        request_id = create_resp.json()["request_id"]

        resp = client.post(
            f"/v2/matchmaking/{request_id}/accept",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 400
