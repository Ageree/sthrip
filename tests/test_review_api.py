"""API integration tests for the Reviews & ZK proof endpoints.

TDD: Tests written first. Run to confirm RED, then implement to GREEN.
"""
import uuid
import pytest
from decimal import Decimal
from unittest.mock import patch

from sthrip.db.models import HubRoute
from sthrip.db.enums import HubRouteStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register(client, name: str) -> str:
    """Register an agent and return its API key."""
    r = client.post("/v2/agents/register", json={"agent_name": name})
    assert r.status_code == 201, f"Registration failed for {name!r}: {r.text}"
    return r.json()["api_key"]


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _create_hub_route(db_session_factory, from_agent_id: uuid.UUID, to_agent_id: uuid.UUID) -> uuid.UUID:
    """Insert a CONFIRMED HubRoute into the test DB and return its id."""
    route_id = uuid.uuid4()
    session = db_session_factory()
    try:
        route = HubRoute(
            id=route_id,
            payment_id=str(uuid.uuid4()).replace("-", "")[:64],
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            amount=Decimal("0.01"),
            fee_amount=Decimal("0.00001"),
            status=HubRouteStatus.CONFIRMED,
        )
        session.add(route)
        session.commit()
    finally:
        session.close()
    return route_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def two_agents(client):
    """Register two agents, return (key_a, key_b, name_a, name_b)."""
    key_a = _register(client, "reviewer-agent")
    key_b = _register(client, "reviewed-agent")
    return key_a, key_b, "reviewer-agent", "reviewed-agent"


@pytest.fixture
def agents_with_transaction(client, db_session_factory, two_agents):
    """Two agents plus a confirmed HubRoute between them.

    Returns (key_a, key_b, name_a, name_b, route_id).
    """
    key_a, key_b, name_a, name_b = two_agents

    # Resolve agent IDs from the DB
    from sthrip.db.models import Agent
    session = db_session_factory()
    try:
        agent_a = session.query(Agent).filter(Agent.agent_name == name_a).first()
        agent_b = session.query(Agent).filter(Agent.agent_name == name_b).first()
        assert agent_a is not None, f"Agent {name_a!r} not found"
        assert agent_b is not None, f"Agent {name_b!r} not found"
        id_a = agent_a.id
        id_b = agent_b.id
    finally:
        session.close()

    route_id = _create_hub_route(db_session_factory, from_agent_id=id_a, to_agent_id=id_b)
    return key_a, key_b, name_a, name_b, route_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCreateReview:
    def test_create_review_201(self, client, agents_with_transaction):
        """Reviewer can leave a review tied to a confirmed payment."""
        key_a, key_b, name_a, name_b, route_id = agents_with_transaction

        r = client.post(
            f"/v2/agents/{name_b}/reviews",
            json={
                "transaction_id": str(route_id),
                "transaction_type": "payment",
                "overall_rating": 5,
                "speed_rating": 4,
                "quality_rating": 5,
                "comment": "Excellent work!",
            },
            headers=_auth(key_a),
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["overall_rating"] == 5
        assert data["transaction_type"] == "payment"

    def test_create_review_self_review_400(self, client, two_agents):
        """An agent cannot review itself."""
        key_a, _key_b, name_a, _name_b = two_agents

        r = client.post(
            f"/v2/agents/{name_a}/reviews",
            json={
                "transaction_id": str(uuid.uuid4()),
                "transaction_type": "payment",
                "overall_rating": 3,
            },
            headers=_auth(key_a),
        )
        assert r.status_code == 400, r.text

    def test_create_review_invalid_transaction_400(self, client, two_agents):
        """Reviewing with a non-existent transaction ID returns 400."""
        key_a, _key_b, name_a, name_b = two_agents
        fake_id = str(uuid.uuid4())

        r = client.post(
            f"/v2/agents/{name_b}/reviews",
            json={
                "transaction_id": fake_id,
                "transaction_type": "payment",
                "overall_rating": 4,
            },
            headers=_auth(key_a),
        )
        assert r.status_code == 400, r.text

    def test_create_review_unauthenticated_401(self, client, two_agents):
        """Unauthenticated request returns 401."""
        _key_a, _key_b, _name_a, name_b = two_agents

        r = client.post(
            f"/v2/agents/{name_b}/reviews",
            json={
                "transaction_id": str(uuid.uuid4()),
                "transaction_type": "payment",
                "overall_rating": 3,
            },
        )
        assert r.status_code == 401, r.text

    def test_create_review_unknown_agent_404(self, client, two_agents):
        """Reviewing a non-existent agent returns 404."""
        key_a, _key_b, _name_a, _name_b = two_agents

        r = client.post(
            "/v2/agents/does-not-exist/reviews",
            json={
                "transaction_id": str(uuid.uuid4()),
                "transaction_type": "payment",
                "overall_rating": 3,
            },
            headers=_auth(key_a),
        )
        assert r.status_code == 404, r.text

    def test_create_review_invalid_rating_422(self, client, agents_with_transaction):
        """Rating outside 1-5 range is rejected at schema level."""
        key_a, _key_b, _name_a, name_b, route_id = agents_with_transaction

        r = client.post(
            f"/v2/agents/{name_b}/reviews",
            json={
                "transaction_id": str(route_id),
                "transaction_type": "payment",
                "overall_rating": 6,  # invalid
            },
            headers=_auth(key_a),
        )
        assert r.status_code == 422, r.text


class TestGetReviews:
    def test_get_reviews(self, client, agents_with_transaction):
        """GET reviews returns a list after a review is created."""
        key_a, _key_b, name_a, name_b, route_id = agents_with_transaction

        # Post a review first
        r = client.post(
            f"/v2/agents/{name_b}/reviews",
            json={
                "transaction_id": str(route_id),
                "transaction_type": "payment",
                "overall_rating": 4,
            },
            headers=_auth(key_a),
        )
        assert r.status_code == 201, r.text

        # Retrieve reviews (public endpoint — no auth required)
        r2 = client.get(f"/v2/agents/{name_b}/reviews")
        assert r2.status_code == 200, r2.text
        data = r2.json()
        assert "reviews" in data
        assert data["total"] >= 1
        assert any(rev["overall_rating"] == 4 for rev in data["reviews"])

    def test_get_reviews_empty(self, client, two_agents):
        """GET reviews for agent with no reviews returns empty list."""
        _key_a, _key_b, _name_a, name_b = two_agents

        r = client.get(f"/v2/agents/{name_b}/reviews")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total"] == 0
        assert data["reviews"] == []

    def test_get_reviews_unknown_agent_404(self, client):
        """GET reviews for unknown agent returns 404."""
        r = client.get("/v2/agents/ghost-agent/reviews")
        assert r.status_code == 404, r.text


class TestGetRatings:
    def test_get_ratings(self, client, agents_with_transaction):
        """GET rating summary updates after a review is posted."""
        key_a, _key_b, name_a, name_b, route_id = agents_with_transaction

        # Post a 5-star review
        r = client.post(
            f"/v2/agents/{name_b}/reviews",
            json={
                "transaction_id": str(route_id),
                "transaction_type": "payment",
                "overall_rating": 5,
            },
            headers=_auth(key_a),
        )
        assert r.status_code == 201, r.text

        r2 = client.get(f"/v2/agents/{name_b}/ratings")
        assert r2.status_code == 200, r2.text
        data = r2.json()
        assert data["total_reviews"] >= 1
        assert float(data["avg_overall"]) > 0

    def test_get_ratings_no_reviews(self, client, two_agents):
        """GET ratings for agent with no reviews returns zeroed summary."""
        _key_a, _key_b, _name_a, name_b = two_agents

        r = client.get(f"/v2/agents/{name_b}/ratings")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total_reviews"] == 0

    def test_get_ratings_unknown_agent_404(self, client):
        """GET ratings for unknown agent returns 404."""
        r = client.get("/v2/agents/ghost-agent/ratings")
        assert r.status_code == 404, r.text


class TestZKReviewProof:
    def test_zk_review_proof_returns_commitment_and_proof(
        self, client, agents_with_transaction
    ):
        """POST /v2/me/review-proof generates a valid ZK proof payload."""
        key_a, _key_b, name_a, name_b, route_id = agents_with_transaction

        # key_b is the "reviewed" agent — post a review so they have ratings
        r = client.post(
            f"/v2/agents/{name_b}/reviews",
            json={
                "transaction_id": str(route_id),
                "transaction_type": "payment",
                "overall_rating": 5,
            },
            headers=_auth(key_a),
        )
        assert r.status_code == 201, r.text

        # Now key_b requests a proof about themselves
        key_b = _key_b
        r2 = client.post(
            "/v2/me/review-proof",
            json={"min_reviews": 1, "min_avg": 1.0},
            headers=_auth(key_b),
        )
        assert r2.status_code == 200, r2.text
        data = r2.json()
        assert "commitment" in data
        assert "proof" in data
        assert data["commitment"] != ""
        assert data["proof"] != ""
        assert data["min_reviews"] == 1

    def test_zk_review_proof_insufficient_reviews_400(
        self, client, two_agents
    ):
        """Requesting a proof when below threshold returns 400."""
        key_a, _key_b, name_a, name_b = two_agents

        # key_a has 0 reviews — request proof requiring 5
        r = client.post(
            "/v2/me/review-proof",
            json={"min_reviews": 5, "min_avg": 4.0},
            headers=_auth(key_a),
        )
        assert r.status_code == 400, r.text

    def test_zk_review_proof_unauthenticated_401(self, client):
        """Proof generation without auth returns 401."""
        r = client.post(
            "/v2/me/review-proof",
            json={"min_reviews": 1, "min_avg": 1.0},
        )
        assert r.status_code == 401, r.text


class TestZKProofVerify:
    def test_zk_proof_verify_valid(self, client, agents_with_transaction):
        """POST /v2/review-proof/verify returns valid=true for a genuine proof."""
        key_a, key_b, name_a, name_b, route_id = agents_with_transaction

        # Post review so key_b has a rating
        r = client.post(
            f"/v2/agents/{name_b}/reviews",
            json={
                "transaction_id": str(route_id),
                "transaction_type": "payment",
                "overall_rating": 5,
            },
            headers=_auth(key_a),
        )
        assert r.status_code == 201, r.text

        # Generate proof as key_b
        r2 = client.post(
            "/v2/me/review-proof",
            json={"min_reviews": 1, "min_avg": 1.0},
            headers=_auth(key_b),
        )
        assert r2.status_code == 200, r2.text
        proof_data = r2.json()

        # Verify the proof (public endpoint, no auth)
        r3 = client.post(
            "/v2/review-proof/verify",
            json={
                "commitment": proof_data["commitment"],
                "proof": proof_data["proof"],
                "min_reviews": 1,
                "min_avg": 1.0,
            },
        )
        assert r3.status_code == 200, r3.text
        result = r3.json()
        assert result["valid"] is True

    def test_zk_proof_verify_tampered_proof_invalid(self, client):
        """Tampered proof returns valid=false (not an error)."""
        r = client.post(
            "/v2/review-proof/verify",
            json={
                "commitment": "aabbcc",
                "proof": "dGhpcyBpcyBub3QgYSByZWFsIHByb29m",  # base64 garbage
                "min_reviews": 1,
                "min_avg": 1.0,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["valid"] is False
