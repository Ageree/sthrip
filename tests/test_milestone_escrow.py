"""Comprehensive tests for multi-milestone escrow.

Tests cover:
  - Multi-milestone creation (5 tests)
  - Full multi-milestone happy path (1 comprehensive test)
  - Milestone state machine (6 tests)
  - Partial release per milestone (2 tests)
  - Permission tests (3 tests)
  - Auto-resolution / expiry (4 tests)
  - Backward compatibility (2 tests)
"""

import os
import contextlib
import pytest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, AgentTier,
    HubRoute, FeeCollection, PendingWithdrawal, Transaction,
    EscrowDeal, EscrowMilestone, EscrowStatus, MilestoneStatus,
)
from sthrip.db.repository import MilestoneRepository
from sthrip.services.escrow_service import EscrowService

# Valid 95-char stagenet XMR address (base58 alphabet, starts with '5')
_VALID_XMR_ADDR = "5" + "a" * 94

# All tables needed for milestone escrow tests
_MILESTONE_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    PendingWithdrawal.__table__,
    Transaction.__table__,
    EscrowDeal.__table__,
    EscrowMilestone.__table__,
]

# Modules where get_db must be patched
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
    "api.routers.escrow",
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
# SQLite timezone compatibility
# ---------------------------------------------------------------------------

def _naive_utc_now() -> datetime:
    """Return current UTC time as a naive datetime (no tzinfo)."""
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Standard 3-milestone definition for reuse across tests
# ---------------------------------------------------------------------------

_THREE_MILESTONES = [
    {
        "description": "Design phase",
        "amount": 0.3,
        "delivery_timeout_hours": 48,
        "review_timeout_hours": 24,
    },
    {
        "description": "Implementation phase",
        "amount": 0.4,
        "delivery_timeout_hours": 72,
        "review_timeout_hours": 24,
    },
    {
        "description": "Final delivery",
        "amount": 0.3,
        "delivery_timeout_hours": 48,
        "review_timeout_hours": 24,
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ms_engine():
    """In-memory SQLite engine with all escrow/milestone tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_MILESTONE_TEST_TABLES)
    return engine


@pytest.fixture
def ms_session_factory(ms_engine):
    """Session factory bound to the milestone test engine."""
    return sessionmaker(bind=ms_engine, expire_on_commit=False)


@pytest.fixture
def escrow_client(ms_engine, ms_session_factory):
    """FastAPI test client with all dependencies mocked."""

    @contextmanager
    def get_test_db():
        session = ms_session_factory()
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
    mock_limiter.check_failed_auth.return_value = None
    mock_limiter.record_failed_auth.return_value = None
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-03-19T00:00:00",
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
            patch(
                "sthrip.services.monitoring.get_monitor",
                return_value=mock_monitor,
            )
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
        stack.enter_context(
            patch("sthrip.services.escrow_service.audit_log")
        )
        stack.enter_context(
            patch("sthrip.services.escrow_service.queue_webhook")
        )
        # SQLite returns naive datetimes; patch _now to match.
        stack.enter_context(
            patch(
                "sthrip.services.escrow_service._now",
                side_effect=_naive_utc_now,
            )
        )

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _register_agent(client: TestClient, name: str) -> str:
    """Register an agent and return its API key."""
    r = client.post("/v2/agents/register", json={
        "agent_name": name,
        "xmr_address": _VALID_XMR_ADDR,
    })
    assert r.status_code == 201, f"Registration of '{name}' failed: {r.text}"
    return r.json()["api_key"]


def _deposit(client: TestClient, api_key: str, amount: float) -> None:
    """Deposit funds into an agent's balance."""
    r = client.post(
        "/v2/balance/deposit",
        json={"amount": amount},
        headers=_auth(api_key),
    )
    assert r.status_code == 200, f"Deposit failed: {r.text}"


def _auth(api_key: str) -> dict:
    """Return auth headers for the given API key."""
    return {"Authorization": f"Bearer {api_key}"}


def _get_balance(client: TestClient, api_key: str) -> Decimal:
    """Return the available balance for an agent."""
    r = client.get("/v2/balance", headers=_auth(api_key))
    assert r.status_code == 200
    return Decimal(r.json()["available"])


def _create_milestone_escrow(
    client: TestClient,
    buyer_key: str,
    seller_name: str,
    amount: float,
    milestones: list,
    description: str = "multi-milestone deal",
) -> dict:
    """Create a multi-milestone escrow and return the response JSON."""
    r = client.post("/v2/escrow", json={
        "seller_agent_name": seller_name,
        "amount": amount,
        "description": description,
        "milestones": milestones,
    }, headers=_auth(buyer_key))
    assert r.status_code == 201, f"Milestone escrow create failed: {r.text}"
    return r.json()


def _create_single_escrow(
    client: TestClient,
    buyer_key: str,
    seller_name: str,
    amount: float,
    description: str = "single deal",
) -> dict:
    """Create a single-milestone (legacy) escrow."""
    r = client.post("/v2/escrow", json={
        "seller_agent_name": seller_name,
        "amount": amount,
        "description": description,
    }, headers=_auth(buyer_key))
    assert r.status_code == 201, f"Single escrow create failed: {r.text}"
    return r.json()


def _accept_escrow(client: TestClient, seller_key: str, escrow_id: str) -> dict:
    """Seller accepts the escrow."""
    r = client.post(f"/v2/escrow/{escrow_id}/accept", headers=_auth(seller_key))
    assert r.status_code == 200, f"Escrow accept failed: {r.text}"
    return r.json()


def _deliver_milestone(
    client: TestClient, seller_key: str, escrow_id: str, seq: int,
) -> dict:
    """Seller delivers a milestone."""
    r = client.post(
        f"/v2/escrow/{escrow_id}/milestones/{seq}/deliver",
        headers=_auth(seller_key),
    )
    assert r.status_code == 200, f"Milestone {seq} deliver failed: {r.text}"
    return r.json()


def _release_milestone(
    client: TestClient, buyer_key: str, escrow_id: str, seq: int,
    release_amount: float,
) -> dict:
    """Buyer releases a milestone."""
    r = client.post(
        f"/v2/escrow/{escrow_id}/milestones/{seq}/release",
        json={"release_amount": release_amount},
        headers=_auth(buyer_key),
    )
    assert r.status_code == 200, f"Milestone {seq} release failed: {r.text}"
    return r.json()


@pytest.fixture
def buyer_seller(escrow_client):
    """Register buyer + seller, fund buyer with 10 XMR."""
    buyer_key = _register_agent(escrow_client, "buyer-agent")
    seller_key = _register_agent(escrow_client, "seller-agent")
    _deposit(escrow_client, buyer_key, 10.0)
    return escrow_client, buyer_key, seller_key


# ---------------------------------------------------------------------------
# 1. Multi-milestone creation (5 tests)
# ---------------------------------------------------------------------------

class TestMultiMilestoneCreation:
    """Validate multi-milestone escrow creation."""

    def test_create_3_milestones_basic(self, buyer_seller):
        """Create escrow with 3 milestones: status=created, is_multi_milestone checks."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        assert created["status"] == "created"
        escrow_id = created["escrow_id"]

        # Verify via GET that is_multi_milestone and milestone_count are set
        r = client.get(f"/v2/escrow/{escrow_id}", headers=_auth(buyer_key))
        assert r.status_code == 200
        data = r.json()
        assert data["is_multi_milestone"] is True
        assert data["milestone_count"] == 3

    def test_milestone_amounts_must_sum_to_deal_amount(self, buyer_seller):
        """Milestone amounts that don't sum to deal amount produce 422."""
        client, buyer_key, seller_key = buyer_seller
        bad_milestones = [
            {
                "description": "Phase 1",
                "amount": 0.3,
                "delivery_timeout_hours": 48,
                "review_timeout_hours": 24,
            },
            {
                "description": "Phase 2",
                "amount": 0.3,
                "delivery_timeout_hours": 48,
                "review_timeout_hours": 24,
            },
        ]
        r = client.post("/v2/escrow", json={
            "seller_agent_name": "seller-agent",
            "amount": 1.0,
            "description": "bad sum",
            "milestones": bad_milestones,
        }, headers=_auth(buyer_key))
        assert r.status_code == 422

    def test_max_10_milestones(self, buyer_seller):
        """More than 10 milestones returns 422."""
        client, buyer_key, seller_key = buyer_seller
        eleven_milestones = [
            {
                "description": f"Phase {i}",
                "amount": 0.1,
                "delivery_timeout_hours": 48,
                "review_timeout_hours": 24,
            }
            for i in range(11)
        ]
        r = client.post("/v2/escrow", json={
            "seller_agent_name": "seller-agent",
            "amount": 1.1,
            "description": "too many milestones",
            "milestones": eleven_milestones,
        }, headers=_auth(buyer_key))
        assert r.status_code == 422

    def test_milestones_have_correct_sequence_numbers(self, buyer_seller):
        """Each milestone gets a sequential number starting at 1."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        r = client.get(
            f"/v2/escrow/{escrow_id}/milestones",
            headers=_auth(buyer_key),
        )
        assert r.status_code == 200
        milestones = r.json()["milestones"]
        assert len(milestones) == 3
        sequences = [m["sequence"] for m in milestones]
        assert sequences == [1, 2, 3]

    def test_single_milestone_no_milestones_param(self, buyer_seller):
        """Single-milestone escrow (no milestones param) still works."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_single_escrow(
            client, buyer_key, "seller-agent", 1.0,
        )
        assert created["status"] == "created"
        escrow_id = created["escrow_id"]

        r = client.get(f"/v2/escrow/{escrow_id}", headers=_auth(buyer_key))
        assert r.status_code == 200
        data = r.json()
        assert data["is_multi_milestone"] is False


# ---------------------------------------------------------------------------
# 2. Full multi-milestone happy path (1 comprehensive test)
# ---------------------------------------------------------------------------

class TestMultiMilestoneHappyPath:
    """Complete 3-milestone lifecycle with partial and full releases."""

    def test_full_3_milestone_lifecycle(self, buyer_seller):
        """
        Create 3-milestone escrow (0.3 + 0.4 + 0.3 = 1.0):
        - Seller accepts
        - Milestone 1: deliver, release full (0.3)
        - Milestone 2: deliver, release partial (0.3 of 0.4)
        - Milestone 3: deliver, release full (0.3)
        - Verify deal completed, verify balances
        """
        client, buyer_key, seller_key = buyer_seller
        fee_pct = Decimal("0.01")

        # Create
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        escrow_id = created["escrow_id"]
        assert _get_balance(client, buyer_key) == Decimal("9")

        # Accept
        accepted = _accept_escrow(client, seller_key, escrow_id)
        assert accepted["status"] == "accepted"

        # Milestone 1: deliver + release full (0.3)
        delivered_1 = _deliver_milestone(client, seller_key, escrow_id, 1)
        assert delivered_1["milestone_status"] == "delivered"
        released_1 = _release_milestone(client, buyer_key, escrow_id, 1, 0.3)
        assert released_1["status"] == "completed"
        assert released_1["deal_status"] == "accepted"  # not last milestone

        # Milestone 2: deliver + release partial (0.3 of 0.4)
        delivered_2 = _deliver_milestone(client, seller_key, escrow_id, 2)
        assert delivered_2["milestone_status"] == "delivered"
        released_2 = _release_milestone(client, buyer_key, escrow_id, 2, 0.3)
        assert released_2["status"] == "completed"
        assert released_2["deal_status"] == "accepted"  # still not last

        # Milestone 3: deliver + release full (0.3)
        delivered_3 = _deliver_milestone(client, seller_key, escrow_id, 3)
        assert delivered_3["milestone_status"] == "delivered"
        released_3 = _release_milestone(client, buyer_key, escrow_id, 3, 0.3)
        assert released_3["status"] == "completed"
        assert released_3["deal_status"] == "completed"  # last milestone

        # Verify deal is completed
        r = client.get(f"/v2/escrow/{escrow_id}", headers=_auth(buyer_key))
        assert r.status_code == 200
        deal = r.json()
        assert deal["status"] == "completed"

        # Calculate expected balances:
        # Total released to seller: 0.3 + 0.3 + 0.3 = 0.9
        # Total fees: 0.9 * 0.01 = 0.009
        # Seller received: 0.9 - 0.009 = 0.891
        # Buyer refund from milestone 2 partial: 0.4 - 0.3 = 0.1
        # Buyer final: 10 - 1.0 + 0.1 = 9.1
        total_released = Decimal("0.3") + Decimal("0.3") + Decimal("0.3")
        total_fees = total_released * fee_pct
        seller_expected = total_released - total_fees
        buyer_expected = Decimal("10") - Decimal("1.0") + Decimal("0.1")

        assert _get_balance(client, seller_key) == seller_expected
        assert _get_balance(client, buyer_key) == buyer_expected


# ---------------------------------------------------------------------------
# 3. Milestone state machine (6 tests)
# ---------------------------------------------------------------------------

class TestMilestoneStateMachine:
    """Milestone state transition enforcement."""

    def test_cannot_deliver_milestone_not_active(self, buyer_seller):
        """Cannot deliver a milestone that is in PENDING state."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        # Milestone #2 is PENDING, should fail
        r = client.post(
            f"/v2/escrow/{escrow_id}/milestones/2/deliver",
            headers=_auth(seller_key),
        )
        assert r.status_code == 400
        assert "active" in r.json()["detail"].lower()

    def test_cannot_release_milestone_not_delivered(self, buyer_seller):
        """Cannot release a milestone that is in ACTIVE (not DELIVERED) state."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        # Milestone #1 is ACTIVE but not DELIVERED
        r = client.post(
            f"/v2/escrow/{escrow_id}/milestones/1/release",
            json={"release_amount": 0.3},
            headers=_auth(buyer_key),
        )
        assert r.status_code == 400
        assert "delivered" in r.json()["detail"].lower()

    def test_cannot_deliver_milestone_2_before_1_completed(self, buyer_seller):
        """Milestone #2 stays PENDING until milestone #1 is completed."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        # Deliver milestone #1 but don't release yet
        _deliver_milestone(client, seller_key, escrow_id, 1)

        # Milestone #2 is still PENDING
        r = client.post(
            f"/v2/escrow/{escrow_id}/milestones/2/deliver",
            headers=_auth(seller_key),
        )
        assert r.status_code == 400

    def test_deliver_on_single_milestone_returns_400(self, buyer_seller):
        """Deliver on single-milestone deal via milestone endpoint returns 400."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_single_escrow(
            client, buyer_key, "seller-agent", 1.0,
        )
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        r = client.post(
            f"/v2/escrow/{escrow_id}/milestones/1/deliver",
            headers=_auth(seller_key),
        )
        assert r.status_code == 400
        assert "milestone" in r.json()["detail"].lower()

    def test_release_on_single_milestone_returns_400(self, buyer_seller):
        """Release on single-milestone deal via milestone endpoint returns 400."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_single_escrow(
            client, buyer_key, "seller-agent", 1.0,
        )
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        r = client.post(
            f"/v2/escrow/{escrow_id}/milestones/1/release",
            json={"release_amount": 1.0},
            headers=_auth(buyer_key),
        )
        assert r.status_code == 400
        assert "milestone" in r.json()["detail"].lower()

    def test_milestone_1_activates_on_accept(self, buyer_seller):
        """After accept, milestone #1 is ACTIVE and milestone #2 is PENDING."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        r = client.get(
            f"/v2/escrow/{escrow_id}/milestones",
            headers=_auth(buyer_key),
        )
        assert r.status_code == 200
        milestones = r.json()["milestones"]
        assert milestones[0]["status"] == "active"
        assert milestones[0]["activated_at"] is not None
        assert milestones[1]["status"] == "pending"
        assert milestones[2]["status"] == "pending"


# ---------------------------------------------------------------------------
# 4. Partial release per milestone (2 tests)
# ---------------------------------------------------------------------------

class TestMilestonePartialRelease:
    """Partial and zero release for individual milestones."""

    def test_zero_release_refunds_milestone_activates_next(self, buyer_seller):
        """Release 0 for a milestone: full refund of that milestone, next activates."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        # Deliver and release milestone #1 with 0
        _deliver_milestone(client, seller_key, escrow_id, 1)
        released = _release_milestone(client, buyer_key, escrow_id, 1, 0.0)
        assert released["status"] == "completed"
        assert Decimal(released["fee"]) == Decimal("0")

        # Buyer should get 0.3 back
        # Balance: 10 - 1.0 + 0.3 = 9.3
        assert _get_balance(client, buyer_key) == Decimal("9.3")

        # Milestone #2 should now be ACTIVE
        r = client.get(
            f"/v2/escrow/{escrow_id}/milestones",
            headers=_auth(buyer_key),
        )
        milestones = r.json()["milestones"]
        assert milestones[1]["status"] == "active"

    def test_partial_release_splits_correctly(self, buyer_seller):
        """Partial release: seller gets partial, buyer gets remainder."""
        client, buyer_key, seller_key = buyer_seller
        fee_pct = Decimal("0.01")
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        # Milestone #1 (amount=0.3): deliver and release 0.2
        _deliver_milestone(client, seller_key, escrow_id, 1)
        released = _release_milestone(client, buyer_key, escrow_id, 1, 0.2)

        expected_fee = Decimal("0.2") * fee_pct
        assert Decimal(released["fee"]) == expected_fee
        assert Decimal(released["released_to_seller"]) == Decimal("0.2")
        seller_received = Decimal("0.2") - expected_fee
        assert Decimal(released["seller_received"]) == seller_received

        # Buyer refund from milestone 1: 0.3 - 0.2 = 0.1
        # Balance: 10 - 1.0 + 0.1 = 9.1
        assert _get_balance(client, buyer_key) == Decimal("9.1")
        assert _get_balance(client, seller_key) == seller_received


# ---------------------------------------------------------------------------
# 5. Permission tests (3 tests)
# ---------------------------------------------------------------------------

class TestMilestonePermissions:
    """Only authorized participants can interact with milestones."""

    def test_non_seller_cannot_deliver_milestone(self, buyer_seller):
        """Non-seller cannot deliver a milestone."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        # Buyer tries to deliver
        r = client.post(
            f"/v2/escrow/{escrow_id}/milestones/1/deliver",
            headers=_auth(buyer_key),
        )
        assert r.status_code == 403

    def test_non_buyer_cannot_release_milestone(self, buyer_seller):
        """Non-buyer cannot release a milestone."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)
        _deliver_milestone(client, seller_key, escrow_id, 1)

        # Seller tries to release
        r = client.post(
            f"/v2/escrow/{escrow_id}/milestones/1/release",
            json={"release_amount": 0.3},
            headers=_auth(seller_key),
        )
        assert r.status_code == 403

    def test_non_participant_cannot_view_milestones(self, buyer_seller):
        """Non-participant cannot view milestones."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        escrow_id = created["escrow_id"]

        third_key = _register_agent(client, "observer-agent")
        r = client.get(
            f"/v2/escrow/{escrow_id}/milestones",
            headers=_auth(third_key),
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# 6. Auto-resolution / expiry (4 tests)
# ---------------------------------------------------------------------------

class TestMilestoneExpiry:
    """Direct unit tests for milestone expiry via EscrowService.resolve_expired."""

    @pytest.fixture
    def svc_session(self, ms_engine, ms_session_factory):
        """Provide (EscrowService, db_session) for direct service-level tests."""
        session = ms_session_factory()
        svc = EscrowService()
        yield svc, session
        session.close()

    @staticmethod
    def _create_agent_pair(db):
        """Insert buyer and seller agents directly into the DB."""
        import uuid

        buyer = Agent(
            id=uuid.uuid4(),
            agent_name="unit-buyer",
            api_key_hash="buyer-hash-" + uuid.uuid4().hex[:8],
            xmr_address=_VALID_XMR_ADDR,
            is_active=True,
            tier=AgentTier.FREE,
        )
        seller = Agent(
            id=uuid.uuid4(),
            agent_name="unit-seller",
            api_key_hash="seller-hash-" + uuid.uuid4().hex[:8],
            xmr_address=_VALID_XMR_ADDR,
            is_active=True,
            tier=AgentTier.FREE,
        )
        db.add(buyer)
        db.add(seller)

        buyer_rep = AgentReputation(agent_id=buyer.id, trust_score=50)
        seller_rep = AgentReputation(agent_id=seller.id, trust_score=50)
        db.add(buyer_rep)
        db.add(seller_rep)

        db.flush()
        return buyer, seller

    @staticmethod
    def _create_balance(db, agent_id, amount: Decimal):
        """Insert a balance record for an agent."""
        bal = AgentBalance(agent_id=agent_id, token="XMR", available=amount)
        db.add(bal)
        db.flush()

    @staticmethod
    def _get_balance_available(db, agent_id) -> Decimal:
        """Read available balance from DB."""
        from sthrip.db.repository import BalanceRepository
        return BalanceRepository(db).get_available(agent_id)

    @staticmethod
    def _milestone_defs():
        """Standard 3-milestone definitions for service-level tests."""
        return [
            {
                "description": "Phase 1",
                "amount": Decimal("0.3"),
                "delivery_timeout_hours": 48,
                "review_timeout_hours": 24,
            },
            {
                "description": "Phase 2",
                "amount": Decimal("0.4"),
                "delivery_timeout_hours": 72,
                "review_timeout_hours": 24,
            },
            {
                "description": "Phase 3",
                "amount": Decimal("0.3"),
                "delivery_timeout_hours": 48,
                "review_timeout_hours": 24,
            },
        ]

    @patch("sthrip.services.escrow_service._now", side_effect=_naive_utc_now)
    @patch("sthrip.services.escrow_service.audit_log")
    @patch("sthrip.services.escrow_service.queue_webhook")
    def test_active_milestone_past_delivery_deadline_expires(
        self, mock_webhook, mock_audit, mock_now, svc_session,
    ):
        """Active milestone past delivery deadline: EXPIRED, remaining cancelled, buyer refunded."""
        svc, db = svc_session
        buyer, seller = self._create_agent_pair(db)
        self._create_balance(db, buyer.id, Decimal("5"))

        result = svc.create_escrow(
            db, buyer.id, seller.id, Decimal("1"),
            description="milestone expiry test",
            milestones=self._milestone_defs(),
        )
        escrow_id = UUID(result["escrow_id"])
        db.commit()

        svc.accept_escrow(db, escrow_id, seller.id)
        db.commit()

        # Verify milestone #1 is ACTIVE
        ms1 = MilestoneRepository(db).get_by_escrow_and_sequence(escrow_id, 1)
        assert ms1.status == MilestoneStatus.ACTIVE

        # Move milestone #1 delivery_deadline to the past
        past = datetime.utcnow() - timedelta(hours=2)
        ms1.delivery_deadline = past
        ms1.expires_at = past
        db.commit()

        # Resolve
        count = svc.resolve_expired(db)
        db.commit()

        assert count == 1

        deal = db.query(EscrowDeal).filter(EscrowDeal.id == escrow_id).first()
        assert deal.status == EscrowStatus.EXPIRED

        # Buyer refunded full 1.0 (no milestones were completed)
        assert self._get_balance_available(db, buyer.id) == Decimal("5")

    @patch("sthrip.services.escrow_service._now", side_effect=_naive_utc_now)
    @patch("sthrip.services.escrow_service.audit_log")
    @patch("sthrip.services.escrow_service.queue_webhook")
    def test_delivered_milestone_past_review_deadline_auto_released(
        self, mock_webhook, mock_audit, mock_now, svc_session,
    ):
        """Delivered milestone past review deadline: auto-released 100% to seller."""
        svc, db = svc_session
        buyer, seller = self._create_agent_pair(db)
        self._create_balance(db, buyer.id, Decimal("5"))
        fee_pct = Decimal("0.01")

        result = svc.create_escrow(
            db, buyer.id, seller.id, Decimal("1"),
            description="auto release test",
            milestones=self._milestone_defs(),
        )
        escrow_id = UUID(result["escrow_id"])
        db.commit()

        svc.accept_escrow(db, escrow_id, seller.id)
        db.commit()

        svc.deliver_milestone(db, escrow_id, 1, seller.id)
        db.commit()

        # Move milestone #1 review_deadline to the past
        ms1 = MilestoneRepository(db).get_by_escrow_and_sequence(escrow_id, 1)
        past = datetime.utcnow() - timedelta(hours=2)
        ms1.review_deadline = past
        ms1.expires_at = past
        db.commit()

        # Resolve
        count = svc.resolve_expired(db)
        db.commit()

        assert count == 1

        # Milestone #1 should be COMPLETED (auto-released)
        ms1_fresh = MilestoneRepository(db).get_by_escrow_and_sequence(escrow_id, 1)
        assert ms1_fresh.status == MilestoneStatus.COMPLETED

        # Seller receives 0.3 - fee
        fee_1 = Decimal("0.3") * fee_pct
        seller_bal = self._get_balance_available(db, seller.id)
        assert seller_bal == Decimal("0.3") - fee_1

        # Deal should still be ACCEPTED (milestones 2 & 3 remain)
        deal = db.query(EscrowDeal).filter(EscrowDeal.id == escrow_id).first()
        assert deal.status == EscrowStatus.ACCEPTED

        # Milestone #2 should now be ACTIVE
        ms2 = MilestoneRepository(db).get_by_escrow_and_sequence(escrow_id, 2)
        assert ms2.status == MilestoneStatus.ACTIVE

    @patch("sthrip.services.escrow_service._now", side_effect=_naive_utc_now)
    @patch("sthrip.services.escrow_service.audit_log")
    @patch("sthrip.services.escrow_service.queue_webhook")
    def test_milestone_1_expires_deal_expired(
        self, mock_webhook, mock_audit, mock_now, svc_session,
    ):
        """Milestone #1 expires -> deal EXPIRED (not PARTIALLY_COMPLETED)."""
        svc, db = svc_session
        buyer, seller = self._create_agent_pair(db)
        self._create_balance(db, buyer.id, Decimal("5"))

        result = svc.create_escrow(
            db, buyer.id, seller.id, Decimal("1"),
            description="milestone 1 expiry test",
            milestones=self._milestone_defs(),
        )
        escrow_id = UUID(result["escrow_id"])
        db.commit()

        svc.accept_escrow(db, escrow_id, seller.id)
        db.commit()

        # Move milestone #1 delivery_deadline to the past
        ms1 = MilestoneRepository(db).get_by_escrow_and_sequence(escrow_id, 1)
        past = datetime.utcnow() - timedelta(hours=2)
        ms1.delivery_deadline = past
        ms1.expires_at = past
        db.commit()

        svc.resolve_expired(db)
        db.commit()

        deal = db.query(EscrowDeal).filter(EscrowDeal.id == escrow_id).first()
        # Milestone 1 is the first -> EXPIRED, not PARTIALLY_COMPLETED
        assert deal.status == EscrowStatus.EXPIRED

        # Seller trust penalized: 50 - 3 = 47
        seller_rep = db.query(AgentReputation).filter(
            AgentReputation.agent_id == seller.id
        ).first()
        assert seller_rep.trust_score == 47

    @patch("sthrip.services.escrow_service._now", side_effect=_naive_utc_now)
    @patch("sthrip.services.escrow_service.audit_log")
    @patch("sthrip.services.escrow_service.queue_webhook")
    def test_milestone_2_expires_after_1_completed_partially_completed(
        self, mock_webhook, mock_audit, mock_now, svc_session,
    ):
        """Milestone #2 expires after #1 completed -> deal PARTIALLY_COMPLETED."""
        svc, db = svc_session
        buyer, seller = self._create_agent_pair(db)
        self._create_balance(db, buyer.id, Decimal("5"))
        fee_pct = Decimal("0.01")

        result = svc.create_escrow(
            db, buyer.id, seller.id, Decimal("1"),
            description="partial completion test",
            milestones=self._milestone_defs(),
        )
        escrow_id = UUID(result["escrow_id"])
        db.commit()

        # Accept and complete milestone #1
        svc.accept_escrow(db, escrow_id, seller.id)
        db.commit()
        svc.deliver_milestone(db, escrow_id, 1, seller.id)
        db.commit()
        svc.release_milestone(
            db, escrow_id, 1, buyer.id, Decimal("0.3"),
        )
        db.commit()

        # Now milestone #2 should be ACTIVE
        ms2 = MilestoneRepository(db).get_by_escrow_and_sequence(escrow_id, 2)
        assert ms2.status == MilestoneStatus.ACTIVE

        # Move milestone #2 delivery_deadline to the past
        past = datetime.utcnow() - timedelta(hours=2)
        ms2.delivery_deadline = past
        ms2.expires_at = past
        db.commit()

        svc.resolve_expired(db)
        db.commit()

        deal = db.query(EscrowDeal).filter(EscrowDeal.id == escrow_id).first()
        # Milestone 2 expires after #1 completed -> PARTIALLY_COMPLETED
        assert deal.status == EscrowStatus.PARTIALLY_COMPLETED

        # Remaining milestones (#3) should be CANCELLED
        ms3 = MilestoneRepository(db).get_by_escrow_and_sequence(escrow_id, 3)
        assert ms3.status == MilestoneStatus.CANCELLED

        # Buyer should be refunded remaining (1.0 - 0.3 released - 0.3*0.001 fees)
        fee_1 = Decimal("0.3") * fee_pct
        remaining = Decimal("1") - Decimal("0.3") - fee_1
        expected_buyer_bal = Decimal("4") + remaining
        assert self._get_balance_available(db, buyer.id) == expected_buyer_bal


# ---------------------------------------------------------------------------
# 7. Backward compatibility (2 tests)
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """Ensure existing single-milestone flow remains unchanged."""

    def test_single_milestone_flow_unchanged(self, buyer_seller):
        """Old single-milestone flow: create -> accept -> deliver -> release works."""
        client, buyer_key, seller_key = buyer_seller

        created = _create_single_escrow(
            client, buyer_key, "seller-agent", 1.0, "legacy deal",
        )
        escrow_id = created["escrow_id"]
        assert created["status"] == "created"

        # Accept
        accepted = _accept_escrow(client, seller_key, escrow_id)
        assert accepted["status"] == "accepted"

        # Deliver (deal-level)
        r = client.post(
            f"/v2/escrow/{escrow_id}/deliver",
            headers=_auth(seller_key),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "delivered"

        # Release (deal-level)
        r = client.post(
            f"/v2/escrow/{escrow_id}/release",
            json={"release_amount": 1.0},
            headers=_auth(buyer_key),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "completed"

        fee = Decimal("1.0") * Decimal("0.01")
        assert Decimal(data["seller_received"]) == Decimal("1.0") - fee
        assert Decimal(data["refunded_to_buyer"]) == Decimal("0")

    def test_get_escrow_includes_milestones_for_multi_milestone(self, buyer_seller):
        """GET /v2/escrow/{id} includes milestones array for multi-milestone deals."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_milestone_escrow(
            client, buyer_key, "seller-agent", 1.0, _THREE_MILESTONES,
        )
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        r = client.get(f"/v2/escrow/{escrow_id}", headers=_auth(buyer_key))
        assert r.status_code == 200
        data = r.json()
        assert data["is_multi_milestone"] is True
        assert "milestones" in data
        assert len(data["milestones"]) == 3
        assert data["milestones"][0]["sequence"] == 1
        assert data["milestones"][0]["status"] == "active"
