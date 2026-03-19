"""Comprehensive integration and unit tests for the escrow system.

Tests cover:
  - Full happy path (create → accept → deliver → release)
  - Partial release and zero release (full refund)
  - Cancellation before accept
  - Auth and permission enforcement
  - Validation (amount bounds, state machine, self-escrow, etc.)
  - List and get endpoints with filtering
  - EscrowService.resolve_expired (accept/delivery/review timeouts)
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
    Base, Agent, AgentReputation, AgentBalance,
    HubRoute, FeeCollection, PendingWithdrawal, Transaction,
    EscrowDeal, EscrowMilestone, EscrowStatus,
)
from sthrip.services.escrow_service import EscrowService

# Valid 95-char stagenet XMR address (base58 alphabet, starts with '5')
_VALID_XMR_ADDR = "5" + "a" * 94

# All tables needed for escrow tests (superset of _COMMON_TEST_TABLES + EscrowDeal)
_ESCROW_TEST_TABLES = [
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

# Modules where get_db must be patched (includes escrow router + service deps).
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
# SQLite timezone compatibility.
#
# SQLite stores datetimes as naive strings. The escrow service uses
# ``datetime.now(timezone.utc)`` for comparisons, which causes TypeError
# when compared against naive values from SQLite.
#
# Fix: patch ``_now`` in the escrow service and in the escrow repo so that
# all internally-generated timestamps are naive-UTC, matching SQLite output.
# ---------------------------------------------------------------------------

def _naive_utc_now() -> datetime:
    """Return current UTC time as a naive datetime (no tzinfo)."""
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def escrow_engine():
    """In-memory SQLite engine with escrow-related tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_ESCROW_TEST_TABLES)
    return engine


@pytest.fixture
def escrow_session_factory(escrow_engine):
    """Session factory bound to the escrow test engine."""
    return sessionmaker(bind=escrow_engine, expire_on_commit=False)


@pytest.fixture
def escrow_client(escrow_engine, escrow_session_factory):
    """FastAPI test client with all dependencies mocked, including escrow tables."""

    @contextmanager
    def get_test_db():
        session = escrow_session_factory()
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
        "timestamp": "2026-03-03T00:00:00",
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
            patch("sthrip.services.escrow_service._now", side_effect=_naive_utc_now)
        )

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


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
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, f"Deposit failed: {r.text}"


def _auth(api_key: str) -> dict:
    """Return auth headers for the given API key."""
    return {"Authorization": f"Bearer {api_key}"}


def _create_escrow(
    client: TestClient,
    buyer_key: str,
    seller_name: str,
    amount: float,
    description: str = "test deal",
) -> dict:
    """Create an escrow and return the response JSON."""
    r = client.post("/v2/escrow", json={
        "seller_agent_name": seller_name,
        "amount": amount,
        "description": description,
    }, headers=_auth(buyer_key))
    assert r.status_code == 201, f"Escrow create failed: {r.text}"
    return r.json()


def _accept_escrow(client: TestClient, seller_key: str, escrow_id: str) -> dict:
    """Seller accepts the escrow."""
    r = client.post(f"/v2/escrow/{escrow_id}/accept", headers=_auth(seller_key))
    assert r.status_code == 200, f"Escrow accept failed: {r.text}"
    return r.json()


def _deliver_escrow(client: TestClient, seller_key: str, escrow_id: str) -> dict:
    """Seller marks the escrow as delivered."""
    r = client.post(f"/v2/escrow/{escrow_id}/deliver", headers=_auth(seller_key))
    assert r.status_code == 200, f"Escrow deliver failed: {r.text}"
    return r.json()


def _release_escrow(
    client: TestClient, buyer_key: str, escrow_id: str, release_amount: float,
) -> dict:
    """Buyer releases funds."""
    r = client.post(f"/v2/escrow/{escrow_id}/release", json={
        "release_amount": release_amount,
    }, headers=_auth(buyer_key))
    assert r.status_code == 200, f"Escrow release failed: {r.text}"
    return r.json()


def _get_balance(client: TestClient, api_key: str) -> Decimal:
    """Return the available balance for an agent."""
    r = client.get("/v2/balance", headers=_auth(api_key))
    assert r.status_code == 200
    return Decimal(r.json()["available"])


@pytest.fixture
def buyer_seller(escrow_client):
    """Register buyer + seller, fund buyer with 10 XMR. Returns (client, buyer_key, seller_key)."""
    buyer_key = _register_agent(escrow_client, "buyer-agent")
    seller_key = _register_agent(escrow_client, "seller-agent")
    _deposit(escrow_client, buyer_key, 10.0)
    return escrow_client, buyer_key, seller_key


# ---------------------------------------------------------------------------
# 1. Full happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    """Complete escrow lifecycle: create → accept → deliver → release (full)."""

    def test_full_lifecycle(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller

        # Create
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        assert created["status"] == "created"
        assert Decimal(created["amount"]) == Decimal("1")
        escrow_id = created["escrow_id"]

        # Buyer balance reduced
        assert _get_balance(client, buyer_key) == Decimal("9")

        # Accept
        accepted = _accept_escrow(client, seller_key, escrow_id)
        assert accepted["status"] == "accepted"
        assert accepted["delivery_deadline"] is not None

        # Deliver
        delivered = _deliver_escrow(client, seller_key, escrow_id)
        assert delivered["status"] == "delivered"
        assert delivered["review_deadline"] is not None

        # Release full amount
        released = _release_escrow(client, buyer_key, escrow_id, 1.0)
        assert released["status"] == "completed"

        fee = Decimal(released["fee"])
        expected_fee = Decimal("1.0") * Decimal("0.001")
        assert fee == expected_fee

        seller_received = Decimal(released["seller_received"])
        assert seller_received == Decimal("1.0") - expected_fee

        refunded = Decimal(released["refunded_to_buyer"])
        assert refunded == Decimal("0")

        # Verify balances
        assert _get_balance(client, buyer_key) == Decimal("9")
        assert _get_balance(client, seller_key) == Decimal("1.0") - expected_fee

    def test_create_returns_correct_fields(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 2.5, "my desc")
        assert "escrow_id" in created
        assert created["seller_agent_name"] == "seller-agent"
        assert created["description"] == "my desc"
        assert created["accept_deadline"] is not None
        assert created["created_at"] is not None


# ---------------------------------------------------------------------------
# 2. Partial release
# ---------------------------------------------------------------------------

class TestPartialRelease:
    """Buyer releases less than the full escrow amount."""

    def test_partial_release_balances(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller

        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)
        _deliver_escrow(client, seller_key, escrow_id)

        released = _release_escrow(client, buyer_key, escrow_id, 0.7)
        assert released["status"] == "completed"

        fee = Decimal(released["fee"])
        expected_fee = Decimal("0.7") * Decimal("0.001")
        assert fee == expected_fee

        seller_received = Decimal(released["seller_received"])
        assert seller_received == Decimal("0.7") - expected_fee

        refunded = Decimal(released["refunded_to_buyer"])
        assert refunded == Decimal("0.3")

        # Buyer: started at 10, escrowed 1, refunded 0.3 → 9.3
        assert _get_balance(client, buyer_key) == Decimal("9.3")

        # Seller: received 0.7 - fee
        assert _get_balance(client, seller_key) == Decimal("0.7") - expected_fee


# ---------------------------------------------------------------------------
# 3. Zero release (full refund)
# ---------------------------------------------------------------------------

class TestZeroRelease:
    """Buyer releases 0 — full refund, no fee charged."""

    def test_zero_release_full_refund(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller

        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)
        _deliver_escrow(client, seller_key, escrow_id)

        released = _release_escrow(client, buyer_key, escrow_id, 0.0)
        assert released["status"] == "completed"
        assert Decimal(released["fee"]) == Decimal("0")
        assert Decimal(released["refunded_to_buyer"]) == Decimal("1")

        # Buyer gets full refund: 10 - 1 + 1 = 10
        assert _get_balance(client, buyer_key) == Decimal("10")

        # Seller receives nothing
        assert _get_balance(client, seller_key) == Decimal("0")


# ---------------------------------------------------------------------------
# 4. Cancel before accept
# ---------------------------------------------------------------------------

class TestCancelBeforeAccept:
    """Buyer cancels before seller accepts."""

    def test_cancel_refunds_buyer(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller

        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]

        # Balance deducted
        assert _get_balance(client, buyer_key) == Decimal("9")

        # Cancel
        r = client.post(
            f"/v2/escrow/{escrow_id}/cancel",
            headers=_auth(buyer_key),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "cancelled"
        assert Decimal(data["refunded"]) == Decimal("1")

        # Full balance restored
        assert _get_balance(client, buyer_key) == Decimal("10")

    def test_accept_after_cancel_fails(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller

        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]
        client.post(f"/v2/escrow/{escrow_id}/cancel", headers=_auth(buyer_key))

        r = client.post(
            f"/v2/escrow/{escrow_id}/accept",
            headers=_auth(seller_key),
        )
        assert r.status_code == 400
        assert "cancelled" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 5. Auth & permission tests
# ---------------------------------------------------------------------------

class TestAuthPermissions:
    """Ensure only authorized participants can perform escrow actions."""

    def test_create_requires_auth(self, escrow_client):
        r = escrow_client.post("/v2/escrow", json={
            "seller_agent_name": "someone",
            "amount": 1.0,
            "description": "test",
        })
        assert r.status_code == 401

    def test_non_seller_cannot_accept(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]

        # Third party tries to accept
        third_key = _register_agent(client, "third-agent")
        r = client.post(
            f"/v2/escrow/{escrow_id}/accept",
            headers=_auth(third_key),
        )
        assert r.status_code == 403

    def test_non_buyer_cannot_release(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)
        _deliver_escrow(client, seller_key, escrow_id)

        r = client.post(
            f"/v2/escrow/{escrow_id}/release",
            json={"release_amount": 1.0},
            headers=_auth(seller_key),
        )
        assert r.status_code == 403

    def test_non_buyer_cannot_cancel(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]

        r = client.post(
            f"/v2/escrow/{escrow_id}/cancel",
            headers=_auth(seller_key),
        )
        assert r.status_code == 403

    def test_non_participant_cannot_view(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]

        third_key = _register_agent(client, "observer-agent")
        r = client.get(
            f"/v2/escrow/{escrow_id}",
            headers=_auth(third_key),
        )
        assert r.status_code == 404

    def test_buyer_can_accept_own_escrow_fails(self, buyer_seller):
        """Buyer cannot accept their own escrow (they are not the seller)."""
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]

        r = client.post(
            f"/v2/escrow/{escrow_id}/accept",
            headers=_auth(buyer_key),
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# 6. Validation tests
# ---------------------------------------------------------------------------

class TestValidation:
    """Input validation and state machine enforcement."""

    def test_cannot_escrow_with_self(self, buyer_seller):
        client, buyer_key, _ = buyer_seller
        r = client.post("/v2/escrow", json={
            "seller_agent_name": "buyer-agent",
            "amount": 1.0,
            "description": "self deal",
        }, headers=_auth(buyer_key))
        assert r.status_code == 400
        assert "yourself" in r.json()["detail"].lower()

    def test_insufficient_balance(self, escrow_client):
        buyer_key = _register_agent(escrow_client, "poor-buyer")
        _register_agent(escrow_client, "rich-seller")
        # No deposit — balance is 0
        r = escrow_client.post("/v2/escrow", json={
            "seller_agent_name": "rich-seller",
            "amount": 1.0,
            "description": "no funds",
        }, headers=_auth(buyer_key))
        assert r.status_code == 400
        assert "insufficient" in r.json()["detail"].lower()

    def test_nonexistent_seller(self, buyer_seller):
        client, buyer_key, _ = buyer_seller
        r = client.post("/v2/escrow", json={
            "seller_agent_name": "ghost-agent",
            "amount": 1.0,
            "description": "no seller",
        }, headers=_auth(buyer_key))
        assert r.status_code == 404

    def test_amount_too_small(self, buyer_seller):
        client, buyer_key, _ = buyer_seller
        r = client.post("/v2/escrow", json={
            "seller_agent_name": "seller-agent",
            "amount": 0.0001,
            "description": "too small",
        }, headers=_auth(buyer_key))
        assert r.status_code == 422  # Pydantic ge=0.001

    def test_amount_too_large(self, buyer_seller):
        client, buyer_key, _ = buyer_seller
        r = client.post("/v2/escrow", json={
            "seller_agent_name": "seller-agent",
            "amount": 99999,
            "description": "too large",
        }, headers=_auth(buyer_key))
        assert r.status_code == 422  # Pydantic le=10000

    def test_cannot_accept_in_accepted_state(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        # Try to accept again
        r = client.post(
            f"/v2/escrow/{escrow_id}/accept",
            headers=_auth(seller_key),
        )
        assert r.status_code == 400
        assert "accepted" in r.json()["detail"].lower()

    def test_cannot_deliver_in_created_state(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]

        r = client.post(
            f"/v2/escrow/{escrow_id}/deliver",
            headers=_auth(seller_key),
        )
        assert r.status_code == 400

    def test_cannot_deliver_in_delivered_state(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)
        _deliver_escrow(client, seller_key, escrow_id)

        r = client.post(
            f"/v2/escrow/{escrow_id}/deliver",
            headers=_auth(seller_key),
        )
        assert r.status_code == 400

    def test_cannot_release_in_created_state(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]

        r = client.post(
            f"/v2/escrow/{escrow_id}/release",
            json={"release_amount": 1.0},
            headers=_auth(buyer_key),
        )
        assert r.status_code == 400

    def test_cannot_release_in_accepted_state(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        r = client.post(
            f"/v2/escrow/{escrow_id}/release",
            json={"release_amount": 1.0},
            headers=_auth(buyer_key),
        )
        assert r.status_code == 400

    def test_release_amount_exceeds_escrow(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)
        _deliver_escrow(client, seller_key, escrow_id)

        r = client.post(
            f"/v2/escrow/{escrow_id}/release",
            json={"release_amount": 2.0},
            headers=_auth(buyer_key),
        )
        assert r.status_code == 400

    def test_release_amount_negative(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)
        _deliver_escrow(client, seller_key, escrow_id)

        r = client.post(
            f"/v2/escrow/{escrow_id}/release",
            json={"release_amount": -0.5},
            headers=_auth(buyer_key),
        )
        # Pydantic ge=0 → 422
        assert r.status_code == 422

    def test_cannot_cancel_after_accept(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]
        _accept_escrow(client, seller_key, escrow_id)

        r = client.post(
            f"/v2/escrow/{escrow_id}/cancel",
            headers=_auth(buyer_key),
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# 7. List & get endpoints
# ---------------------------------------------------------------------------

class TestListAndGet:
    """Escrow listing and detail endpoints."""

    def test_list_as_buyer(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        _create_escrow(client, buyer_key, "seller-agent", 0.5)
        _create_escrow(client, buyer_key, "seller-agent", 0.3)

        r = client.get(
            "/v2/escrow?role=buyer",
            headers=_auth(buyer_key),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    def test_list_as_seller(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        _create_escrow(client, buyer_key, "seller-agent", 0.5)

        r = client.get(
            "/v2/escrow?role=seller",
            headers=_auth(seller_key),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1

    def test_list_filtered_by_status(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller

        created1 = _create_escrow(client, buyer_key, "seller-agent", 0.5)
        created2 = _create_escrow(client, buyer_key, "seller-agent", 0.3)
        _accept_escrow(client, seller_key, created1["escrow_id"])

        # Filter for "created" status
        r = client.get(
            "/v2/escrow?status=created",
            headers=_auth(buyer_key),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["escrow_id"] == created2["escrow_id"]

    def test_list_all_roles(self, buyer_seller):
        """role=all returns escrows where agent is buyer or seller."""
        client, buyer_key, seller_key = buyer_seller
        _create_escrow(client, buyer_key, "seller-agent", 0.5)

        r = client.get(
            "/v2/escrow?role=all",
            headers=_auth(buyer_key),
        )
        assert r.status_code == 200
        assert r.json()["total"] == 1

    def test_get_detail_as_buyer(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0, "detail test")
        escrow_id = created["escrow_id"]

        r = client.get(
            f"/v2/escrow/{escrow_id}",
            headers=_auth(buyer_key),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["escrow_id"] == escrow_id
        assert data["description"] == "detail test"
        assert data["buyer_agent_name"] == "buyer-agent"
        assert data["seller_agent_name"] == "seller-agent"

    def test_get_detail_as_seller(self, buyer_seller):
        client, buyer_key, seller_key = buyer_seller
        created = _create_escrow(client, buyer_key, "seller-agent", 1.0)
        escrow_id = created["escrow_id"]

        r = client.get(
            f"/v2/escrow/{escrow_id}",
            headers=_auth(seller_key),
        )
        assert r.status_code == 200
        assert r.json()["escrow_id"] == escrow_id

    def test_get_nonexistent_escrow(self, buyer_seller):
        client, buyer_key, _ = buyer_seller
        fake_id = "00000000-0000-0000-0000-000000000000"
        r = client.get(
            f"/v2/escrow/{fake_id}",
            headers=_auth(buyer_key),
        )
        assert r.status_code == 404

    def test_list_empty(self, escrow_client):
        key = _register_agent(escrow_client, "lonely-agent")
        r = escrow_client.get("/v2/escrow", headers=_auth(key))
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["items"] == []


# ---------------------------------------------------------------------------
# 8. EscrowService.resolve_expired unit tests
# ---------------------------------------------------------------------------

class TestResolveExpired:
    """Direct unit tests for EscrowService.resolve_expired background task."""

    @pytest.fixture
    def svc_session(self, escrow_engine, escrow_session_factory):
        """Provide (EscrowService, db_session) for direct service-level tests."""
        session = escrow_session_factory()
        svc = EscrowService()
        yield svc, session
        session.close()

    @staticmethod
    def _create_agent_pair(db):
        """Insert buyer and seller agents directly into the DB.

        Returns (buyer, seller) Agent objects.
        """
        from sthrip.db.models import AgentTier
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

    @patch("sthrip.services.escrow_service._now", side_effect=_naive_utc_now)
    @patch("sthrip.services.escrow_service.audit_log")
    @patch("sthrip.services.escrow_service.queue_webhook")
    def test_accept_timeout_refunds_buyer(self, mock_webhook, mock_audit, mock_now, svc_session):
        """CREATED past accept_deadline → EXPIRED, buyer refunded."""
        svc, db = svc_session
        buyer, seller = self._create_agent_pair(db)
        self._create_balance(db, buyer.id, Decimal("5"))

        # Create escrow (deducts from buyer)
        result = svc.create_escrow(
            db, buyer.id, seller.id, Decimal("1"),
            description="accept timeout test",
            accept_timeout_hours=1,
        )
        escrow_id = UUID(result["escrow_id"])
        db.commit()

        # Balance after create: 5 - 1 = 4
        assert self._get_balance_available(db, buyer.id) == Decimal("4")

        # Manipulate deadline to the past
        deal = db.query(EscrowDeal).filter(EscrowDeal.id == escrow_id).first()
        past = datetime.utcnow() - timedelta(hours=2)
        deal.accept_deadline = past
        deal.expires_at = past
        db.commit()

        # Resolve
        count = svc.resolve_expired(db)
        db.commit()

        assert count == 1

        deal = db.query(EscrowDeal).filter(EscrowDeal.id == escrow_id).first()
        assert deal.status == EscrowStatus.EXPIRED

        # Buyer refunded: 4 + 1 = 5
        assert self._get_balance_available(db, buyer.id) == Decimal("5")

    @patch("sthrip.services.escrow_service._now", side_effect=_naive_utc_now)
    @patch("sthrip.services.escrow_service.audit_log")
    @patch("sthrip.services.escrow_service.queue_webhook")
    def test_delivery_timeout_refunds_buyer_penalizes_seller(
        self, mock_webhook, mock_audit, mock_now, svc_session,
    ):
        """ACCEPTED past delivery_deadline → EXPIRED, buyer refunded, seller trust -3."""
        svc, db = svc_session
        buyer, seller = self._create_agent_pair(db)
        self._create_balance(db, buyer.id, Decimal("5"))

        result = svc.create_escrow(
            db, buyer.id, seller.id, Decimal("2"),
            description="delivery timeout test",
        )
        escrow_id = UUID(result["escrow_id"])
        db.commit()

        # Accept
        svc.accept_escrow(db, escrow_id, seller.id)
        db.commit()

        # Manipulate delivery deadline to the past
        deal = db.query(EscrowDeal).filter(EscrowDeal.id == escrow_id).first()
        past = datetime.utcnow() - timedelta(hours=2)
        deal.delivery_deadline = past
        deal.expires_at = past
        db.commit()

        # Resolve
        count = svc.resolve_expired(db)
        db.commit()

        assert count == 1

        deal = db.query(EscrowDeal).filter(EscrowDeal.id == escrow_id).first()
        assert deal.status == EscrowStatus.EXPIRED

        # Buyer refunded: 5 - 2 + 2 = 5
        assert self._get_balance_available(db, buyer.id) == Decimal("5")

        # Seller trust: 50 - 3 = 47
        seller_rep = db.query(AgentReputation).filter(
            AgentReputation.agent_id == seller.id
        ).first()
        assert seller_rep.trust_score == 47

    @patch("sthrip.services.escrow_service._now", side_effect=_naive_utc_now)
    @patch("sthrip.services.escrow_service.audit_log")
    @patch("sthrip.services.escrow_service.queue_webhook")
    def test_review_timeout_auto_releases_to_seller(
        self, mock_webhook, mock_audit, mock_now, svc_session,
    ):
        """DELIVERED past review_deadline → COMPLETED, 100% released to seller (minus fee)."""
        svc, db = svc_session
        buyer, seller = self._create_agent_pair(db)
        self._create_balance(db, buyer.id, Decimal("5"))

        result = svc.create_escrow(
            db, buyer.id, seller.id, Decimal("1"),
            description="review timeout test",
        )
        escrow_id = UUID(result["escrow_id"])
        db.commit()

        svc.accept_escrow(db, escrow_id, seller.id)
        db.commit()
        svc.deliver_escrow(db, escrow_id, seller.id)
        db.commit()

        # Manipulate review deadline to the past
        deal = db.query(EscrowDeal).filter(EscrowDeal.id == escrow_id).first()
        past = datetime.utcnow() - timedelta(hours=2)
        deal.review_deadline = past
        deal.expires_at = past
        db.commit()

        # Resolve
        count = svc.resolve_expired(db)
        db.commit()

        assert count == 1

        deal = db.query(EscrowDeal).filter(EscrowDeal.id == escrow_id).first()
        assert deal.status == EscrowStatus.COMPLETED

        fee = Decimal("1") * Decimal("0.001")
        expected_seller_balance = Decimal("1") - fee

        assert self._get_balance_available(db, seller.id) == expected_seller_balance

        # Buyer: 5 - 1 = 4 (no refund on auto-release)
        assert self._get_balance_available(db, buyer.id) == Decimal("4")

    @patch("sthrip.services.escrow_service._now", side_effect=_naive_utc_now)
    @patch("sthrip.services.escrow_service.audit_log")
    @patch("sthrip.services.escrow_service.queue_webhook")
    def test_resolve_expired_with_no_expired_deals(
        self, mock_webhook, mock_audit, mock_now, svc_session,
    ):
        """resolve_expired returns 0 when no deals are past deadline."""
        svc, db = svc_session
        count = svc.resolve_expired(db)
        assert count == 0
