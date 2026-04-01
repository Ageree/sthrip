"""Integration tests for Discovery API v2 — enhanced marketplace filters.

TDD: Tests written BEFORE implementation (RED phase).
Covers:
  - min_rating filter
  - min_reviews filter
  - max_price filter (based on SLA template base_price)
  - has_sla filter
  - sort=rating ordering
  - response includes `rating` object
  - response includes `sla_templates` array
"""

import os
import contextlib
import uuid
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base,
    Agent,
    AgentReputation,
    AgentBalance,
    AgentRatingSummary,
    HubRoute,
    FeeCollection,
    PendingWithdrawal,
    Transaction,
    SpendingPolicy,
    WebhookEndpoint,
    MessageRelay,
    EscrowDeal,
    EscrowMilestone,
    MultisigEscrow,
    MultisigRound,
    SLATemplate,
    SLAContract,
    AgentReview,
)

# Valid 95-char stagenet XMR address (starts with '5', base58 alphabet).
_VALID_XMR_ADDR = "5" + "a" * 94

_COMMON_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    PendingWithdrawal.__table__,
    Transaction.__table__,
    SpendingPolicy.__table__,
    WebhookEndpoint.__table__,
    MessageRelay.__table__,
    EscrowDeal.__table__,
    EscrowMilestone.__table__,
    MultisigEscrow.__table__,
    MultisigRound.__table__,
    SLATemplate.__table__,
    SLAContract.__table__,
    AgentReview.__table__,
    AgentRatingSummary.__table__,
]

_GET_DB_MODULES = [
    "sthrip.db.database",
    "sthrip.services.agent_registry",
    "sthrip.services.fee_collector",
    "sthrip.services.webhook_service",
    "api.main_v2",
    "api.deps",
    "api.routers.health",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.webhooks",
    "api.routers.spending_policy",
    "api.routers.webhook_endpoints",
    "api.routers.reputation",
    "api.routers.messages",
    "api.routers.multisig_escrow",
    "api.routers.escrow",
    "api.routers.sla",
    "api.routers.reviews",
]

_RATE_LIMITER_MODULES = [
    "sthrip.services.rate_limiter",
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
]

_AUDIT_LOG_MODULES = [
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.admin",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def discovery_engine():
    """In-memory SQLite engine with all required tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_COMMON_TEST_TABLES)
    return engine


@pytest.fixture
def discovery_session_factory(discovery_engine):
    """Session factory bound to the discovery test engine."""
    return sessionmaker(bind=discovery_engine, expire_on_commit=False)


@pytest.fixture
def discovery_client_and_session(discovery_engine, discovery_session_factory):
    """Yields (client, session_factory) so tests can insert records directly.

    The client's get_db override and the helper session factory share the
    same in-memory SQLite engine, so records inserted via the session are
    immediately visible to the API handler.
    """

    @contextmanager
    def get_test_db():
        session = discovery_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    mock_limiter = MagicMock()
    mock_limiter.check_rate_limit.return_value = None
    mock_limiter.check_ip_rate_limit.return_value = None
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-04-01T00:00:00",
        "checks": {},
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger"}))

        for mod in _GET_DB_MODULES:
            stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))
        stack.enter_context(patch("sthrip.db.database.create_tables"))

        for mod in _RATE_LIMITER_MODULES:
            stack.enter_context(
                patch(f"{mod}.get_rate_limiter", return_value=mock_limiter)
            )

        for mod in _AUDIT_LOG_MODULES:
            stack.enter_context(patch(f"{mod}.audit_log"))

        stack.enter_context(
            patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor)
        )
        stack.enter_context(
            patch(
                "sthrip.services.monitoring.setup_default_monitoring",
                return_value=mock_monitor,
            )
        )
        stack.enter_context(
            patch(
                "sthrip.services.webhook_service.get_webhook_service",
                return_value=mock_webhook,
            )
        )
        stack.enter_context(
            patch("sthrip.services.webhook_service.queue_webhook")
        )

        from api.main_v2 import app

        yield TestClient(app, raise_server_exceptions=False), discovery_session_factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register(client: TestClient, name: str, **kwargs) -> dict:
    """Register an agent and return the full JSON response."""
    payload = {"agent_name": name, "xmr_address": _VALID_XMR_ADDR, **kwargs}
    r = client.post("/v2/agents/register", json=payload)
    assert r.status_code == 201, f"Registration of '{name}' failed: {r.text}"
    return r.json()


def _get_agent_id(client: TestClient, agent_name: str) -> str:
    """Return the agent_id for the named agent via the profile endpoint."""
    r = client.get(f"/v2/agents/{agent_name}")
    assert r.status_code == 200, f"Profile lookup for '{agent_name}' failed: {r.text}"
    # The profile endpoint does not expose agent_id directly, so we reach into
    # the DB via a separate session in each test that needs it.
    return r.json()


def _insert_rating_summary(
    session_factory,
    agent_id: uuid.UUID,
    avg_overall: float,
    total_reviews: int,
    avg_speed: float = 0.0,
    avg_quality: float = 0.0,
    avg_reliability: float = 0.0,
    five_star_count: int = 0,
    one_star_count: int = 0,
) -> None:
    """Insert (or replace) an AgentRatingSummary row directly."""
    session = session_factory()
    try:
        existing = (
            session.query(AgentRatingSummary)
            .filter(AgentRatingSummary.agent_id == agent_id)
            .first()
        )
        if existing is None:
            summary = AgentRatingSummary(
                agent_id=agent_id,
                avg_overall=Decimal(str(avg_overall)),
                total_reviews=total_reviews,
                avg_speed=Decimal(str(avg_speed)),
                avg_quality=Decimal(str(avg_quality)),
                avg_reliability=Decimal(str(avg_reliability)),
                five_star_count=five_star_count,
                one_star_count=one_star_count,
            )
            session.add(summary)
        else:
            existing.avg_overall = Decimal(str(avg_overall))
            existing.total_reviews = total_reviews
            existing.avg_speed = Decimal(str(avg_speed))
            existing.avg_quality = Decimal(str(avg_quality))
            existing.avg_reliability = Decimal(str(avg_reliability))
            existing.five_star_count = five_star_count
            existing.one_star_count = one_star_count
        session.commit()
    finally:
        session.close()


def _insert_sla_template(
    session_factory,
    provider_id: uuid.UUID,
    name: str,
    base_price: float,
    delivery_time_secs: int = 3600,
    response_time_secs: int = 600,
    penalty_percent: int = 10,
    is_active: bool = True,
) -> uuid.UUID:
    """Insert an SLATemplate row and return its id."""
    session = session_factory()
    try:
        template = SLATemplate(
            provider_id=provider_id,
            name=name,
            service_description=f"Service for {name}",
            deliverables=[],
            response_time_secs=response_time_secs,
            delivery_time_secs=delivery_time_secs,
            base_price=Decimal(str(base_price)),
            currency="XMR",
            penalty_percent=penalty_percent,
            is_active=is_active,
        )
        session.add(template)
        session.commit()
        return template.id
    finally:
        session.close()


def _get_agent_db_id(session_factory, agent_name: str) -> uuid.UUID:
    """Look up an agent's UUID by name."""
    session = session_factory()
    try:
        agent = (
            session.query(Agent).filter(Agent.agent_name == agent_name).first()
        )
        assert agent is not None, f"Agent '{agent_name}' not found in DB"
        return agent.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Tests: Enhanced marketplace filters
# ---------------------------------------------------------------------------

class TestMarketplaceFilterMinRating:
    """?min_rating=X filters out agents whose avg_overall is below threshold."""

    def test_filter_min_rating(self, discovery_client_and_session):
        """Only agents with avg_overall >= min_rating appear in results."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-high-rated")
        _register(client, "dv2-low-rated")

        high_id = _get_agent_db_id(sf, "dv2-high-rated")
        low_id = _get_agent_db_id(sf, "dv2-low-rated")

        _insert_rating_summary(sf, high_id, avg_overall=4.5, total_reviews=5)
        _insert_rating_summary(sf, low_id, avg_overall=2.0, total_reviews=5)

        r = client.get("/v2/agents/marketplace?min_rating=4.0")
        assert r.status_code == 200, r.text
        data = r.json()
        names = [item["agent_name"] for item in data["items"]]

        assert "dv2-high-rated" in names
        assert "dv2-low-rated" not in names

    def test_filter_min_rating_no_summary_excluded(self, discovery_client_and_session):
        """Agents without any rating summary are excluded when min_rating is set."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-no-summary")

        r = client.get("/v2/agents/marketplace?min_rating=3.0")
        assert r.status_code == 200, r.text
        data = r.json()
        names = [item["agent_name"] for item in data["items"]]

        assert "dv2-no-summary" not in names

    def test_filter_min_rating_boundary_included(self, discovery_client_and_session):
        """An agent with avg_overall exactly equal to min_rating is included."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-exact-rating")
        exact_id = _get_agent_db_id(sf, "dv2-exact-rating")
        _insert_rating_summary(sf, exact_id, avg_overall=4.0, total_reviews=1)

        r = client.get("/v2/agents/marketplace?min_rating=4.0")
        assert r.status_code == 200, r.text
        names = [item["agent_name"] for item in r.json()["items"]]
        assert "dv2-exact-rating" in names


class TestMarketplaceFilterMinReviews:
    """?min_reviews=N filters out agents with fewer than N reviews."""

    def test_filter_min_reviews(self, discovery_client_and_session):
        """Agents with total_reviews < min_reviews are excluded."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-many-reviews")
        _register(client, "dv2-few-reviews")

        many_id = _get_agent_db_id(sf, "dv2-many-reviews")
        few_id = _get_agent_db_id(sf, "dv2-few-reviews")

        _insert_rating_summary(sf, many_id, avg_overall=4.0, total_reviews=15)
        _insert_rating_summary(sf, few_id, avg_overall=4.0, total_reviews=3)

        r = client.get("/v2/agents/marketplace?min_reviews=10")
        assert r.status_code == 200, r.text
        names = [item["agent_name"] for item in r.json()["items"]]

        assert "dv2-many-reviews" in names
        assert "dv2-few-reviews" not in names

    def test_filter_min_reviews_no_summary_excluded(self, discovery_client_and_session):
        """Agents without rating summary are excluded when min_reviews is set."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-no-rev-summary")

        r = client.get("/v2/agents/marketplace?min_reviews=1")
        assert r.status_code == 200, r.text
        names = [item["agent_name"] for item in r.json()["items"]]
        assert "dv2-no-rev-summary" not in names


class TestMarketplaceFilterMaxPrice:
    """?max_price=X filters agents based on their cheapest active SLA template."""

    def test_filter_max_price(self, discovery_client_and_session):
        """Agents with cheapest SLA > max_price are excluded."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-cheap-agent")
        _register(client, "dv2-expensive-agent")
        _register(client, "dv2-no-sla-agent")

        cheap_id = _get_agent_db_id(sf, "dv2-cheap-agent")
        expensive_id = _get_agent_db_id(sf, "dv2-expensive-agent")

        _insert_sla_template(sf, cheap_id, name="budget-plan", base_price=0.5)
        _insert_sla_template(sf, expensive_id, name="premium-plan", base_price=5.0)

        r = client.get("/v2/agents/marketplace?max_price=1.0")
        assert r.status_code == 200, r.text
        names = [item["agent_name"] for item in r.json()["items"]]

        assert "dv2-cheap-agent" in names
        assert "dv2-expensive-agent" not in names
        # Agents with no SLA at all are excluded when max_price is specified
        assert "dv2-no-sla-agent" not in names

    def test_filter_max_price_boundary_included(self, discovery_client_and_session):
        """An agent with base_price exactly equal to max_price is included."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-boundary-price")
        boundary_id = _get_agent_db_id(sf, "dv2-boundary-price")
        _insert_sla_template(sf, boundary_id, name="exact-price", base_price=1.0)

        r = client.get("/v2/agents/marketplace?max_price=1.0")
        assert r.status_code == 200, r.text
        names = [item["agent_name"] for item in r.json()["items"]]
        assert "dv2-boundary-price" in names


class TestMarketplaceFilterHasSla:
    """?has_sla=true only returns agents with at least one active SLA template."""

    def test_filter_has_sla_true(self, discovery_client_and_session):
        """?has_sla=true excludes agents with no active SLA templates."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-with-sla")
        _register(client, "dv2-without-sla")

        with_id = _get_agent_db_id(sf, "dv2-with-sla")
        _insert_sla_template(sf, with_id, name="basic-sla", base_price=0.1)

        r = client.get("/v2/agents/marketplace?has_sla=true")
        assert r.status_code == 200, r.text
        names = [item["agent_name"] for item in r.json()["items"]]

        assert "dv2-with-sla" in names
        assert "dv2-without-sla" not in names

    def test_filter_has_sla_inactive_template_excluded(self, discovery_client_and_session):
        """An agent with only inactive SLA templates is excluded by ?has_sla=true."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-inactive-sla")
        inactive_id = _get_agent_db_id(sf, "dv2-inactive-sla")
        _insert_sla_template(sf, inactive_id, name="old-plan", base_price=0.5, is_active=False)

        r = client.get("/v2/agents/marketplace?has_sla=true")
        assert r.status_code == 200, r.text
        names = [item["agent_name"] for item in r.json()["items"]]
        assert "dv2-inactive-sla" not in names

    def test_filter_has_sla_false_returns_agents_without_sla(self, discovery_client_and_session):
        """?has_sla=false returns agents with no active SLA templates."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-sla-false-yes")
        _register(client, "dv2-sla-false-no")

        yes_id = _get_agent_db_id(sf, "dv2-sla-false-yes")
        _insert_sla_template(sf, yes_id, name="some-sla", base_price=0.5)

        r = client.get("/v2/agents/marketplace?has_sla=false")
        assert r.status_code == 200, r.text
        names = [item["agent_name"] for item in r.json()["items"]]

        assert "dv2-sla-false-no" in names
        assert "dv2-sla-false-yes" not in names


class TestMarketplaceSortByRating:
    """?sort=rating orders results by avg_overall descending."""

    def test_sort_by_rating(self, discovery_client_and_session):
        """Agents are returned in descending avg_overall order when sort=rating."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-sort-mid")
        _register(client, "dv2-sort-top")
        _register(client, "dv2-sort-bot")

        mid_id = _get_agent_db_id(sf, "dv2-sort-mid")
        top_id = _get_agent_db_id(sf, "dv2-sort-top")
        bot_id = _get_agent_db_id(sf, "dv2-sort-bot")

        _insert_rating_summary(sf, mid_id, avg_overall=3.5, total_reviews=2)
        _insert_rating_summary(sf, top_id, avg_overall=5.0, total_reviews=2)
        _insert_rating_summary(sf, bot_id, avg_overall=1.5, total_reviews=2)

        r = client.get("/v2/agents/marketplace?sort=rating")
        assert r.status_code == 200, r.text
        names = [item["agent_name"] for item in r.json()["items"]]

        # Agents with ratings present; they must appear in descending order
        rated = [n for n in names if n in ("dv2-sort-top", "dv2-sort-mid", "dv2-sort-bot")]
        assert rated == ["dv2-sort-top", "dv2-sort-mid", "dv2-sort-bot"]


class TestMarketplaceResponseShape:
    """Marketplace items include `rating` and `sla_templates` fields."""

    def test_response_includes_rating(self, discovery_client_and_session):
        """Each marketplace item includes a `rating` dict with expected keys."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-rating-shape")
        agent_id = _get_agent_db_id(sf, "dv2-rating-shape")
        _insert_rating_summary(
            sf, agent_id,
            avg_overall=4.7, total_reviews=156,
            avg_speed=4.5, avg_quality=4.9, avg_reliability=4.8,
        )

        r = client.get("/v2/agents/marketplace")
        assert r.status_code == 200, r.text
        items = r.json()["items"]

        target = next(
            (item for item in items if item["agent_name"] == "dv2-rating-shape"), None
        )
        assert target is not None, "Expected agent not in marketplace response"
        assert "rating" in target, "Response item missing `rating` field"

        rating = target["rating"]
        assert "overall" in rating
        assert "total_reviews" in rating
        assert "speed" in rating
        assert "quality" in rating
        assert "reliability" in rating

        assert rating["total_reviews"] == 156
        assert abs(float(rating["overall"]) - 4.7) < 0.05

    def test_response_includes_sla_templates(self, discovery_client_and_session):
        """Each marketplace item includes a `sla_templates` list."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-sla-shape")
        agent_id = _get_agent_db_id(sf, "dv2-sla-shape")
        _insert_sla_template(
            sf, agent_id, name="starter",
            base_price=0.5, delivery_time_secs=3600, penalty_percent=10,
        )

        r = client.get("/v2/agents/marketplace")
        assert r.status_code == 200, r.text
        items = r.json()["items"]

        target = next(
            (item for item in items if item["agent_name"] == "dv2-sla-shape"), None
        )
        assert target is not None
        assert "sla_templates" in target, "Response item missing `sla_templates` field"
        assert isinstance(target["sla_templates"], list)

        if target["sla_templates"]:
            tpl = target["sla_templates"][0]
            assert "name" in tpl
            assert "price" in tpl
            assert "delivery_time_secs" in tpl
            assert "penalty_percent" in tpl

    def test_response_rating_none_when_no_summary(self, discovery_client_and_session):
        """An agent with no rating summary returns rating=None in response."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-no-rating")

        r = client.get("/v2/agents/marketplace")
        assert r.status_code == 200, r.text
        items = r.json()["items"]

        target = next(
            (item for item in items if item["agent_name"] == "dv2-no-rating"), None
        )
        assert target is not None
        # rating field must be present (may be None or empty dict)
        assert "rating" in target
        assert target["rating"] is None

    def test_response_sla_templates_empty_when_none(self, discovery_client_and_session):
        """An agent with no SLA templates returns sla_templates=[] in response."""
        client, sf = discovery_client_and_session

        _register(client, "dv2-no-sla-shape")

        r = client.get("/v2/agents/marketplace")
        assert r.status_code == 200, r.text
        items = r.json()["items"]

        target = next(
            (item for item in items if item["agent_name"] == "dv2-no-sla-shape"), None
        )
        assert target is not None
        assert "sla_templates" in target
        assert target["sla_templates"] == []


class TestMarketplaceCombinedFilters:
    """Multiple filters applied simultaneously."""

    def test_combined_min_rating_and_has_sla(self, discovery_client_and_session):
        """?min_rating=4.0&has_sla=true returns only agents meeting both criteria."""
        client, sf = discovery_client_and_session

        # Agent A: good rating + has SLA -> should appear
        _register(client, "dv2-combo-a")
        a_id = _get_agent_db_id(sf, "dv2-combo-a")
        _insert_rating_summary(sf, a_id, avg_overall=4.5, total_reviews=5)
        _insert_sla_template(sf, a_id, name="plan-a", base_price=0.5)

        # Agent B: good rating, no SLA -> should not appear
        _register(client, "dv2-combo-b")
        b_id = _get_agent_db_id(sf, "dv2-combo-b")
        _insert_rating_summary(sf, b_id, avg_overall=4.5, total_reviews=5)

        # Agent C: has SLA but low rating -> should not appear
        _register(client, "dv2-combo-c")
        c_id = _get_agent_db_id(sf, "dv2-combo-c")
        _insert_rating_summary(sf, c_id, avg_overall=2.0, total_reviews=5)
        _insert_sla_template(sf, c_id, name="plan-c", base_price=0.5)

        r = client.get("/v2/agents/marketplace?min_rating=4.0&has_sla=true")
        assert r.status_code == 200, r.text
        names = [item["agent_name"] for item in r.json()["items"]]

        assert "dv2-combo-a" in names
        assert "dv2-combo-b" not in names
        assert "dv2-combo-c" not in names
