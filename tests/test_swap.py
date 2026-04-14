"""
Cross-chain swap tests — TDD approach (RED → GREEN → REFACTOR).

Unit tests:  SwapRepository and SwapService in isolation.
API tests:   /v2/swap/* endpoints via FastAPI TestClient.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    HubRoute, FeeCollection, PendingWithdrawal, Transaction,
    SpendingPolicy, WebhookEndpoint, MessageRelay,
    EscrowDeal, EscrowMilestone, MultisigEscrow, MultisigRound,
    SLATemplate, SLAContract,
    AgentReview, AgentRatingSummary,
    MatchRequest, RecurringPayment,
    PaymentChannel, ChannelUpdate, PaymentStream,
    SwapOrder, SwapStatus,
)
from sthrip.db.swap_repo import SwapRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SWAP_TEST_TABLES = [
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
    MatchRequest.__table__,
    RecurringPayment.__table__,
    PaymentChannel.__table__,
    ChannelUpdate.__table__,
    PaymentStream.__table__,
    SwapOrder.__table__,
]


@pytest.fixture
def swap_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_SWAP_TEST_TABLES)
    return engine


@pytest.fixture
def swap_session_factory(swap_engine):
    return sessionmaker(bind=swap_engine, expire_on_commit=False)


@pytest.fixture
def db(swap_session_factory):
    session = swap_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _make_agent(db, name: str = None) -> Agent:
    """Create and persist a minimal Agent for tests."""
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=name or f"agent-{uuid.uuid4().hex[:8]}",
        api_key_hash="testhash",
        is_active=True,
    )
    db.add(agent)
    db.flush()
    return agent


def _valid_htlc_pair():
    """Return (secret_hex, hash_hex) pair."""
    secret = secrets.token_hex(32)
    h = hashlib.sha256(bytes.fromhex(secret)).hexdigest()
    return secret, h


# ---------------------------------------------------------------------------
# UNIT TESTS — SwapRepository
# ---------------------------------------------------------------------------


class TestSwapRepository:
    """Unit tests for SwapRepository data-access layer."""

    def test_create_swap_order(self, db):
        """create() persists a SwapOrder in CREATED state with all fields."""
        agent = _make_agent(db)
        secret, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )

        assert order.id is not None
        assert order.from_agent_id == agent.id
        assert order.from_currency == "BTC"
        assert order.from_amount == Decimal("0.01")
        assert order.to_currency == "XMR"
        assert order.to_amount == Decimal("1.5")
        assert order.exchange_rate == Decimal("150.0")
        assert order.fee_amount == Decimal("0.015")
        assert order.htlc_hash == htlc_hash
        assert order.state == SwapStatus.CREATED
        assert order.htlc_secret is None
        assert order.btc_tx_hash is None

    def test_get_by_id_returns_order(self, db):
        """get_by_id() returns the SwapOrder for a valid id."""
        agent = _make_agent(db)
        _, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        repo = SwapRepository(db)
        created = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()

        found = repo.get_by_id(created.id)
        assert found is not None
        assert found.id == created.id

    def test_get_by_id_returns_none_for_unknown(self, db):
        """get_by_id() returns None when the id does not exist."""
        repo = SwapRepository(db)
        result = repo.get_by_id(uuid.uuid4())
        assert result is None

    def test_lock_transitions_created_to_locked(self, db):
        """lock() transitions state CREATED → LOCKED and stores btc_tx_hash."""
        agent = _make_agent(db)
        _, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()

        rows = repo.lock(order.id, btc_tx_hash="abc123btctxhash")
        db.commit()

        assert rows == 1
        db.refresh(order)
        assert order.state == SwapStatus.LOCKED
        assert order.btc_tx_hash == "abc123btctxhash"

    def test_lock_is_idempotent_on_wrong_state(self, db):
        """lock() returns 0 rows if the order is not in CREATED state."""
        agent = _make_agent(db)
        _, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()
        repo.lock(order.id, btc_tx_hash="first")
        db.commit()

        # Second lock should affect 0 rows
        rows = repo.lock(order.id, btc_tx_hash="second")
        assert rows == 0

    def test_complete_transitions_locked_to_completed(self, db):
        """complete() transitions state LOCKED → COMPLETED, stores secret."""
        agent = _make_agent(db)
        secret, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()
        repo.lock(order.id, btc_tx_hash="btctxhash")
        db.commit()

        rows = repo.complete(order.id, htlc_secret=secret, xmr_tx_hash="xmrtxhash")
        db.commit()

        assert rows == 1
        db.refresh(order)
        assert order.state == SwapStatus.COMPLETED
        assert order.htlc_secret == secret
        assert order.xmr_tx_hash == "xmrtxhash"

    def test_complete_returns_zero_if_not_locked(self, db):
        """complete() returns 0 when order is in CREATED (not LOCKED) state."""
        agent = _make_agent(db)
        secret, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()

        rows = repo.complete(order.id, htlc_secret=secret)
        assert rows == 0

    def test_refund_transitions_locked_to_refunded(self, db):
        """refund() transitions state LOCKED → REFUNDED."""
        agent = _make_agent(db)
        _, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()
        repo.lock(order.id, btc_tx_hash="btctxhash")
        db.commit()

        rows = repo.refund(order.id)
        db.commit()

        assert rows == 1
        db.refresh(order)
        assert order.state == SwapStatus.REFUNDED

    def test_refund_returns_zero_if_not_locked(self, db):
        """refund() returns 0 when order is not in LOCKED state."""
        agent = _make_agent(db)
        _, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()

        rows = repo.refund(order.id)
        assert rows == 0

    def test_expire_transitions_created_to_expired(self, db):
        """expire() transitions CREATED → EXPIRED."""
        agent = _make_agent(db)
        _, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()

        rows = repo.expire(order.id)
        db.commit()

        assert rows == 1
        db.refresh(order)
        assert order.state == SwapStatus.EXPIRED

    def test_expire_transitions_locked_to_expired(self, db):
        """expire() transitions LOCKED → EXPIRED."""
        agent = _make_agent(db)
        _, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()
        repo.lock(order.id, btc_tx_hash="btctxhash")
        db.commit()

        rows = repo.expire(order.id)
        db.commit()

        assert rows == 1
        db.refresh(order)
        assert order.state == SwapStatus.EXPIRED

    def test_expire_returns_zero_if_completed(self, db):
        """expire() returns 0 when order is already COMPLETED."""
        agent = _make_agent(db)
        secret, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()
        repo.lock(order.id, btc_tx_hash="btctx")
        db.commit()
        repo.complete(order.id, htlc_secret=secret)
        db.commit()

        rows = repo.expire(order.id)
        assert rows == 0

    def test_get_expired_returns_past_deadline_orders(self, db):
        """get_expired() returns orders whose lock_expiry is in the past."""
        agent = _make_agent(db)
        _, htlc_hash = _valid_htlc_pair()

        repo = SwapRepository(db)
        # Past-deadline order
        past_expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
        stale_order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=past_expiry,
        )
        # Future-deadline order
        _, htlc_hash2 = _valid_htlc_pair()
        future_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
        fresh_order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash2,
            lock_expiry=future_expiry,
        )
        db.commit()

        expired = repo.get_expired()

        expired_ids = [o.id for o in expired]
        assert stale_order.id in expired_ids
        assert fresh_order.id not in expired_ids

    def test_get_expired_excludes_terminal_states(self, db):
        """get_expired() excludes COMPLETED / REFUNDED / already-EXPIRED orders."""
        agent = _make_agent(db)
        secret, htlc_hash = _valid_htlc_pair()

        repo = SwapRepository(db)
        past_expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=past_expiry,
        )
        db.commit()
        repo.lock(order.id, btc_tx_hash="btctx")
        db.commit()
        repo.complete(order.id, htlc_secret=secret)
        db.commit()

        expired = repo.get_expired()
        assert order.id not in [o.id for o in expired]

    def test_list_by_agent_returns_agents_orders(self, db):
        """list_by_agent() returns orders for the agent and correct total."""
        agent = _make_agent(db)
        other = _make_agent(db)
        repo = SwapRepository(db)
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        for _ in range(3):
            _, h = _valid_htlc_pair()
            repo.create(
                from_agent_id=agent.id,
                from_currency="BTC",
                from_amount=Decimal("0.01"),
                to_currency="XMR",
                to_amount=Decimal("1.5"),
                exchange_rate=Decimal("150.0"),
                fee_amount=Decimal("0.015"),
                htlc_hash=h,
                lock_expiry=lock_expiry,
            )

        _, h2 = _valid_htlc_pair()
        repo.create(
            from_agent_id=other.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=h2,
            lock_expiry=lock_expiry,
        )
        db.commit()

        items, total = repo.list_by_agent(agent.id, limit=50, offset=0)
        assert total == 3
        assert len(items) == 3
        for item in items:
            assert item.from_agent_id == agent.id

    def test_list_by_agent_respects_limit_and_offset(self, db):
        """list_by_agent() applies limit and offset correctly."""
        agent = _make_agent(db)
        repo = SwapRepository(db)
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        for _ in range(5):
            _, h = _valid_htlc_pair()
            repo.create(
                from_agent_id=agent.id,
                from_currency="BTC",
                from_amount=Decimal("0.01"),
                to_currency="XMR",
                to_amount=Decimal("1.5"),
                exchange_rate=Decimal("150.0"),
                fee_amount=Decimal("0.015"),
                htlc_hash=h,
                lock_expiry=lock_expiry,
            )
        db.commit()

        items, total = repo.list_by_agent(agent.id, limit=2, offset=1)
        assert total == 5
        assert len(items) == 2

    def test_list_by_agent_empty_for_unknown_agent(self, db):
        """list_by_agent() returns empty list for agent with no swaps."""
        repo = SwapRepository(db)
        items, total = repo.list_by_agent(uuid.uuid4(), limit=50, offset=0)
        assert items == []
        assert total == 0


# ---------------------------------------------------------------------------
# UNIT TESTS — HTLC hash verification logic
# ---------------------------------------------------------------------------


class TestHTLCHashVerification:
    """Tests for HTLC secret/hash verification used in SwapService.claim_swap."""

    def test_htlc_hash_verification_correct_secret(self):
        """SHA-256 of secret bytes matches stored htlc_hash."""
        secret = secrets.token_hex(32)
        htlc_hash = hashlib.sha256(bytes.fromhex(secret)).hexdigest()

        computed = hashlib.sha256(bytes.fromhex(secret)).hexdigest()
        assert computed == htlc_hash

    def test_htlc_hash_verification_wrong_secret(self):
        """A different secret does NOT produce the same htlc_hash."""
        secret = secrets.token_hex(32)
        htlc_hash = hashlib.sha256(bytes.fromhex(secret)).hexdigest()

        wrong_secret = secrets.token_hex(32)
        computed = hashlib.sha256(bytes.fromhex(wrong_secret)).hexdigest()
        assert computed != htlc_hash

    def test_claim_wrong_secret_raises_value_error(self, db):
        """SwapService.claim_swap raises ValueError when the provided secret is wrong."""
        from sthrip.services.swap_service import SwapService

        agent = _make_agent(db)
        secret, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()

        svc = SwapService()
        wrong_secret = secrets.token_hex(32)
        with pytest.raises(ValueError, match="[Ii]nvalid.*secret"):
            svc.claim_swap(db, order_id=order.id, agent_id=agent.id, htlc_secret=wrong_secret)


# ---------------------------------------------------------------------------
# UNIT TESTS — SwapService (with mocked RateService)
# ---------------------------------------------------------------------------

_MOCK_RATES = {
    "BTC_XMR": Decimal("150.0"),
    "XMR_BTC": Decimal("0.006667"),
    "ETH_XMR": Decimal("10.0"),
    "XMR_USD": Decimal("180.0"),
    "XMR_EUR": Decimal("160.0"),
}

_MOCK_QUOTE = {
    "from_currency": "BTC",
    "to_currency": "XMR",
    "from_amount": "0.01",
    "to_amount": "1.485",
    "rate": "150.0",
    "fee": "0.015",
    "expires_in": 300,
}


@pytest.fixture
def mock_rate_service():
    with patch("sthrip.services.swap_service.RateService") as MockClass:
        instance = MagicMock()
        instance.get_rates.return_value = _MOCK_RATES
        instance.get_quote.return_value = _MOCK_QUOTE
        MockClass.return_value = instance
        yield instance


class TestSwapService:
    """Unit tests for SwapService (rate service mocked)."""

    def test_get_rates_delegates_to_rate_service(self, db, mock_rate_service):
        """get_rates() returns the dict from RateService.get_rates()."""
        from sthrip.services.swap_service import SwapService

        svc = SwapService()
        rates = svc.get_rates()
        assert rates == _MOCK_RATES
        mock_rate_service.get_rates.assert_called_once()

    def test_get_quote_delegates_to_rate_service(self, db, mock_rate_service):
        """get_quote() returns the dict from RateService.get_quote()."""
        from sthrip.services.swap_service import SwapService

        svc = SwapService()
        quote = svc.get_quote("BTC", Decimal("0.01"), "XMR")
        assert quote == _MOCK_QUOTE
        mock_rate_service.get_quote.assert_called_once_with("BTC", Decimal("0.01"), "XMR")

    def test_create_swap_returns_order_dict(self, db, mock_rate_service):
        """create_swap() returns a dict containing swap_id and state."""
        from sthrip.services.swap_service import SwapService

        agent = _make_agent(db)
        svc = SwapService()
        result = svc.create_swap(db, from_agent_id=agent.id, from_currency="BTC", from_amount=Decimal("0.01"))

        assert "swap_id" in result
        assert result["state"] == "created"
        assert result["from_currency"] == "BTC"
        assert result["to_currency"] == "XMR"
        # htlc_secret must NOT be returned for security
        assert "htlc_secret" not in result

    def test_create_swap_stores_htlc_hash_and_secret_in_db(self, db, mock_rate_service):
        """create_swap() stores htlc_hash (64-char SHA-256 hex) and the pre-image
        secret in the DB so the initiator can retrieve it for the claim step.
        The secret is intentionally NOT included in the create response dict."""
        from sthrip.services.swap_service import SwapService

        agent = _make_agent(db)
        svc = SwapService()
        result = svc.create_swap(db, from_agent_id=agent.id, from_currency="BTC", from_amount=Decimal("0.01"))

        repo = SwapRepository(db)
        order = repo.get_by_id(uuid.UUID(result["swap_id"]))
        assert order is not None
        assert order.htlc_hash is not None
        assert len(order.htlc_hash) == 64  # SHA-256 hex string
        # Secret stored so the initiator can use it for the claim step
        assert order.htlc_secret is not None
        assert len(order.htlc_secret) == 64  # 32-byte hex pre-image
        # The response dict must NOT expose the secret (security requirement)
        assert "htlc_secret" not in result

    def test_create_swap_sets_30_minute_lock_expiry(self, db, mock_rate_service):
        """create_swap() sets lock_expiry approximately 30 minutes from now."""
        from sthrip.services.swap_service import SwapService

        agent = _make_agent(db)
        before = datetime.now(timezone.utc)
        svc = SwapService()
        result = svc.create_swap(db, from_agent_id=agent.id, from_currency="BTC", from_amount=Decimal("0.01"))
        after = datetime.now(timezone.utc)

        repo = SwapRepository(db)
        order = repo.get_by_id(uuid.UUID(result["swap_id"]))
        assert order is not None
        expected_min = before + timedelta(minutes=29, seconds=50)
        expected_max = after + timedelta(minutes=30, seconds=10)
        lock_expiry = order.lock_expiry
        if lock_expiry.tzinfo is None:
            lock_expiry = lock_expiry.replace(tzinfo=timezone.utc)
        assert expected_min <= lock_expiry <= expected_max

    def test_get_swap_returns_dict_for_owner(self, db, mock_rate_service):
        """get_swap() returns order dict when called by the owning agent."""
        from sthrip.services.swap_service import SwapService

        agent = _make_agent(db)
        svc = SwapService()
        created = svc.create_swap(db, from_agent_id=agent.id, from_currency="BTC", from_amount=Decimal("0.01"))
        db.commit()

        order_dict = svc.get_swap(db, order_id=uuid.UUID(created["swap_id"]), agent_id=agent.id)
        assert order_dict["swap_id"] == created["swap_id"]

    def test_get_swap_raises_permission_error_for_wrong_agent(self, db, mock_rate_service):
        """get_swap() raises PermissionError when called by a different agent."""
        from sthrip.services.swap_service import SwapService

        agent = _make_agent(db)
        other = _make_agent(db)
        svc = SwapService()
        created = svc.create_swap(db, from_agent_id=agent.id, from_currency="BTC", from_amount=Decimal("0.01"))
        db.commit()

        with pytest.raises(PermissionError):
            svc.get_swap(db, order_id=uuid.UUID(created["swap_id"]), agent_id=other.id)

    def test_get_swap_raises_lookup_error_for_unknown_id(self, db, mock_rate_service):
        """get_swap() raises LookupError for non-existent swap_id."""
        from sthrip.services.swap_service import SwapService

        agent = _make_agent(db)
        svc = SwapService()
        with pytest.raises(LookupError):
            svc.get_swap(db, order_id=uuid.uuid4(), agent_id=agent.id)

    def test_claim_swap_completes_order_and_credits_balance(self, db, mock_rate_service):
        """claim_swap() completes the order and credits XMR balance to the agent."""
        from sthrip.services.swap_service import SwapService
        from sthrip.db.repository import BalanceRepository

        agent = _make_agent(db)
        svc = SwapService()
        created = svc.create_swap(db, from_agent_id=agent.id, from_currency="BTC", from_amount=Decimal("0.01"))
        db.commit()

        # Retrieve the secret from the internal order (simulate the actual flow where
        # the caller received the secret at creation time in a real HTLC flow —
        # here we read it from the DB for the test since create_swap holds it).
        repo = SwapRepository(db)
        order = repo.get_by_id(uuid.UUID(created["swap_id"]))
        # We need to simulate the full flow: lock first, then claim.
        repo.lock(order.id, btc_tx_hash="btctxhash123")
        db.commit()
        db.refresh(order)

        # Read the secret that was generated during create_swap via the service
        # (the service stores it internally for the HTLC).
        # The service stores it as a field on a transient attribute or in the order.
        # We get the secret by re-fetching and using the stored hash to derive nothing —
        # instead we need to get the HTLC secret from the service directly.
        # Since create_swap stores the secret in the order's htlc_secret transiently
        # (before claiming), we check the returned dict for a dedicated field.
        # Per spec: htlc_secret is NOT in the create response.
        # So we need to simulate: the SwapService internally holds the secret.
        # For testing, we read it from the order's htlc_secret (stored by create_swap
        # in the DB for later claim verification — the spec says "store secret").
        # Actually, let's check what create_swap actually does:
        # It generates secret, stores htlc_hash, and returns the secret in
        # a separate private channel. Since we can't access that, we query the DB
        # where the service temporarily stores the pre-image.
        # Per the spec, the service stores htlc_secret in the order at creation time
        # so the caller can use it. We retrieve it from the DB record.
        # (In production the secret would be returned securely to the swap initiator.)
        stored_secret = order.htlc_secret  # stored at creation for HTLC pre-image

        result = svc.claim_swap(db, order_id=order.id, agent_id=agent.id, htlc_secret=stored_secret)
        db.commit()

        assert result["state"] == "completed"
        balance_repo = BalanceRepository(db)
        available = balance_repo.get_available(agent.id, "XMR")
        # to_amount from mock quote is "1.485"
        assert available == Decimal("1.485")

    def test_expire_stale_expires_overdue_orders(self, db, mock_rate_service):
        """expire_stale() expires all overdue CREATED/LOCKED orders."""
        from sthrip.services.swap_service import SwapService

        agent = _make_agent(db)
        repo = SwapRepository(db)
        past_expiry = datetime.now(timezone.utc) - timedelta(minutes=1)

        _, h1 = _valid_htlc_pair()
        o1 = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=h1,
            lock_expiry=past_expiry,
        )
        _, h2 = _valid_htlc_pair()
        o2 = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=h2,
            lock_expiry=past_expiry,
        )
        db.commit()

        svc = SwapService()
        count = svc.expire_stale(db)
        db.commit()

        assert count == 2
        db.refresh(o1)
        db.refresh(o2)
        assert o1.state == SwapStatus.EXPIRED
        assert o2.state == SwapStatus.EXPIRED


# ---------------------------------------------------------------------------
# API INTEGRATION TESTS
# ---------------------------------------------------------------------------

import contextlib
import os


_SWAP_GET_DB_MODULES = [
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
    "api.routers.matchmaking",
    "api.routers.channels",
    "api.routers.subscriptions",
    "api.routers.streams",
    "api.routers.swap",
]

_SWAP_AUDIT_MODULES = [
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.admin",
]

_SWAP_RATE_LIMITER_MODULES = [
    "sthrip.services.rate_limiter",
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
]


@pytest.fixture
def api_client(swap_engine, swap_session_factory):
    """TestClient with all dependencies patched for swap integration tests."""
    from unittest.mock import patch, MagicMock
    import contextlib

    @contextlib.contextmanager
    def get_test_db():
        session = swap_session_factory()
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
    mock_limiter.check_failed_auth.return_value = None
    mock_limiter.record_failed_auth.return_value = None

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-03-03T00:00:00",
        "checks": {},
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    _TEST_ENCRYPTION_KEY = "uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE="

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "HUB_MODE": "ledger",
            "ADMIN_API_KEY": "test-admin-key-for-tests-long-enough-32",
            "ENVIRONMENT": "dev",
            "DATABASE_URL": "sqlite:///:memory:",
            "WEBHOOK_ENCRYPTION_KEY": _TEST_ENCRYPTION_KEY,
        }))

        for mod in _SWAP_GET_DB_MODULES:
            stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))
        stack.enter_context(patch("sthrip.db.database.create_tables"))

        for mod in _SWAP_RATE_LIMITER_MODULES:
            stack.enter_context(
                patch(f"{mod}.get_rate_limiter", return_value=mock_limiter)
            )

        for mod in _SWAP_AUDIT_MODULES:
            stack.enter_context(patch(f"{mod}.audit_log"))

        stack.enter_context(
            patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor)
        )
        stack.enter_context(
            patch("sthrip.services.monitoring.setup_default_monitoring", return_value=mock_monitor)
        )
        stack.enter_context(
            patch("sthrip.services.webhook_service.get_webhook_service", return_value=mock_webhook)
        )
        stack.enter_context(patch("sthrip.services.webhook_service.queue_webhook"))

        # Patch RateService in swap_service to use deterministic rates
        mock_rate_svc = MagicMock()
        mock_rate_svc.get_rates.return_value = _MOCK_RATES
        mock_rate_svc.get_quote.return_value = _MOCK_QUOTE
        stack.enter_context(
            patch("sthrip.services.swap_service.RateService", return_value=mock_rate_svc)
        )

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


def _register_agent_and_get_key(client: TestClient, name: str = None) -> tuple:
    """Register an agent and return (agent_id, api_key)."""
    agent_name = name or f"swap-agent-{uuid.uuid4().hex[:8]}"
    resp = client.post("/v2/agents/register", json={
        "agent_name": agent_name,
        "webhook_url": None,
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return data["agent_id"], data["api_key"]


class TestSwapAPI:
    """Integration tests for /v2/swap/* endpoints."""

    def test_get_rates_200(self, api_client):
        """GET /v2/swap/rates returns 200 with supported pairs (public endpoint)."""
        resp = api_client.get("/v2/swap/rates")
        assert resp.status_code == 200
        body = resp.json()
        assert "BTC_XMR" in body

    def test_get_quote_200(self, api_client):
        """POST /v2/swap/quote returns 200 with quote details (auth required)."""
        agent_id, api_key = _register_agent_and_get_key(api_client)
        resp = api_client.post(
            "/v2/swap/quote",
            json={"from_currency": "BTC", "from_amount": "0.01", "to_currency": "XMR"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "rate" in body
        assert "to_amount" in body

    def test_get_quote_requires_auth(self, api_client):
        """POST /v2/swap/quote returns 401 without authentication."""
        resp = api_client.post(
            "/v2/swap/quote",
            json={"from_currency": "BTC", "from_amount": "0.01", "to_currency": "XMR"},
        )
        assert resp.status_code == 401

    def test_create_swap_201(self, api_client):
        """POST /v2/swap/create returns 201 with swap_id and state=created."""
        agent_id, api_key = _register_agent_and_get_key(api_client)
        resp = api_client.post(
            "/v2/swap/create",
            json={"from_currency": "BTC", "from_amount": "0.01"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "swap_id" in body
        assert body["state"] == "created"
        assert "htlc_secret" not in body  # security: not exposed in create response

    def test_create_swap_requires_auth(self, api_client):
        """POST /v2/swap/create returns 401 without authentication."""
        resp = api_client.post(
            "/v2/swap/create",
            json={"from_currency": "BTC", "from_amount": "0.01"},
        )
        assert resp.status_code == 401

    def test_get_swap_200(self, api_client):
        """GET /v2/swap/{swap_id} returns 200 for the owning agent."""
        agent_id, api_key = _register_agent_and_get_key(api_client)
        create_resp = api_client.post(
            "/v2/swap/create",
            json={"from_currency": "BTC", "from_amount": "0.01"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert create_resp.status_code == 201, create_resp.text
        swap_id = create_resp.json()["swap_id"]

        resp = api_client.get(
            f"/v2/swap/{swap_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["swap_id"] == swap_id

    def test_get_swap_404_for_unknown_id(self, api_client):
        """GET /v2/swap/{swap_id} returns 404 for a non-existent swap."""
        agent_id, api_key = _register_agent_and_get_key(api_client)
        resp = api_client.get(
            f"/v2/swap/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 404

    def test_claim_swap_200(self, api_client, swap_session_factory):
        """POST /v2/swap/{swap_id}/claim returns 200 with state=completed."""
        agent_id, api_key = _register_agent_and_get_key(api_client)
        create_resp = api_client.post(
            "/v2/swap/create",
            json={"from_currency": "BTC", "from_amount": "0.01"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert create_resp.status_code == 201, create_resp.text
        swap_id = create_resp.json()["swap_id"]

        # Lock the swap first (simulate BTC tx detected)
        with swap_session_factory() as db_session:
            repo = SwapRepository(db_session)
            order = repo.get_by_id(uuid.UUID(swap_id))
            assert order is not None
            repo.lock(order.id, btc_tx_hash="btctxhash123")
            stored_secret = order.htlc_secret
            db_session.commit()

        resp = api_client.post(
            f"/v2/swap/{swap_id}/claim",
            json={"htlc_secret": stored_secret},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "completed"

    def test_claim_swap_400_wrong_secret(self, api_client, swap_session_factory):
        """POST /v2/swap/{swap_id}/claim returns 400 with wrong HTLC secret."""
        agent_id, api_key = _register_agent_and_get_key(api_client)
        create_resp = api_client.post(
            "/v2/swap/create",
            json={"from_currency": "BTC", "from_amount": "0.01"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert create_resp.status_code == 201, create_resp.text
        swap_id = create_resp.json()["swap_id"]

        # Lock the swap
        with swap_session_factory() as db_session:
            repo = SwapRepository(db_session)
            order = repo.get_by_id(uuid.UUID(swap_id))
            repo.lock(order.id, btc_tx_hash="btctxhash123")
            db_session.commit()

        wrong_secret = secrets.token_hex(32)
        resp = api_client.post(
            f"/v2/swap/{swap_id}/claim",
            json={"htlc_secret": wrong_secret},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# SPRINT 2 — SwapRepository: set_external_order + get_pending_external
# ---------------------------------------------------------------------------


class TestSwapRepositoryExternalOrder:
    """Tests for the new exchange-provider fields on SwapRepository."""

    def test_set_external_order_stores_fields(self, db):
        """set_external_order() stores provider fields and returns 1 row."""
        agent = _make_agent(db)
        _, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()

        rows = repo.set_external_order(
            swap_id=order.id,
            external_order_id="cn-ext-001",
            deposit_address="bc1qdeposit123",
            provider_name="changenow",
        )
        db.commit()

        assert rows == 1
        db.refresh(order)
        assert order.external_order_id == "cn-ext-001"
        assert order.deposit_address == "bc1qdeposit123"
        assert order.provider_name == "changenow"

    def test_set_external_order_returns_zero_for_non_created_order(self, db):
        """set_external_order() returns 0 if the order is not in CREATED state."""
        agent = _make_agent(db)
        secret, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()
        # Move to LOCKED state
        repo.lock(order.id, btc_tx_hash="btctx")
        db.commit()

        rows = repo.set_external_order(
            swap_id=order.id,
            external_order_id="cn-ext-002",
            deposit_address="bc1qdeposit456",
            provider_name="changenow",
        )
        assert rows == 0

    def test_get_pending_external_returns_orders_with_external_id(self, db):
        """get_pending_external() returns only CREATED orders with external_order_id set."""
        agent = _make_agent(db)
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
        repo = SwapRepository(db)

        # Order with external_order_id set
        _, h1 = _valid_htlc_pair()
        order_with = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=h1,
            lock_expiry=lock_expiry,
        )
        db.commit()
        repo.set_external_order(
            swap_id=order_with.id,
            external_order_id="ext-001",
            deposit_address="bc1q...",
            provider_name="changenow",
        )
        db.commit()

        # Order without external_order_id
        _, h2 = _valid_htlc_pair()
        order_without = repo.create(
            from_agent_id=agent.id,
            from_currency="ETH",
            from_amount=Decimal("0.5"),
            to_currency="XMR",
            to_amount=Decimal("5.0"),
            exchange_rate=Decimal("10.0"),
            fee_amount=Decimal("0.005"),
            htlc_hash=h2,
            lock_expiry=lock_expiry,
        )
        db.commit()

        pending = repo.get_pending_external()
        pending_ids = [o.id for o in pending]
        assert order_with.id in pending_ids
        assert order_without.id not in pending_ids

    def test_get_pending_external_excludes_non_created(self, db):
        """get_pending_external() excludes LOCKED/COMPLETED orders even if they have external_order_id."""
        agent = _make_agent(db)
        _, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()
        repo.set_external_order(
            swap_id=order.id,
            external_order_id="ext-locked",
            deposit_address="addr",
            provider_name="changenow",
        )
        db.commit()
        repo.lock(order.id, btc_tx_hash="btctx")
        db.commit()

        pending = repo.get_pending_external()
        assert order.id not in [o.id for o in pending]


# ---------------------------------------------------------------------------
# SPRINT 2 — SwapService: create_swap with real exchange providers
# ---------------------------------------------------------------------------


class TestSwapServiceWithExchangeProviders:
    """Tests for create_swap using mocked exchange providers."""

    def test_create_swap_calls_exchange_and_stores_deposit_address(
        self, db, swap_session_factory
    ):
        """create_swap() stores deposit_address when exchange provider succeeds."""
        from unittest.mock import patch
        from sthrip.services.swap_service import SwapService

        provider_result = {
            "external_order_id": "cn-test-id",
            "deposit_address": "bc1qtest_deposit_address",
            "expected_amount": "0.01",
            "provider": "changenow",
        }
        agent = _make_agent(db)
        db.commit()

        svc = SwapService()
        with patch(
            "sthrip.services.swap_service.create_order_with_fallback",
            return_value=provider_result,
        ), patch.object(
            svc, "_get_hub_xmr_address", return_value="4AbCdEfHub"
        ):
            result = svc.create_swap(
                db, from_agent_id=agent.id, from_currency="BTC", from_amount=Decimal("0.01")
            )

        assert result["deposit_address"] == "bc1qtest_deposit_address"
        assert result["external_order_id"] == "cn-test-id"
        assert result["provider_name"] == "changenow"
        assert "htlc_secret" not in result

    def test_create_swap_succeeds_when_exchange_provider_fails(self, db):
        """create_swap() returns order without deposit_address if provider fails."""
        from unittest.mock import patch
        from sthrip.services.swap_service import SwapService
        from sthrip.services.exchange_providers import ExchangeProviderError

        agent = _make_agent(db)
        db.commit()

        svc = SwapService()
        with patch(
            "sthrip.services.swap_service.create_order_with_fallback",
            side_effect=ExchangeProviderError("all providers failed"),
        ), patch.object(
            svc, "_get_hub_xmr_address", return_value="4AbCdEfHub"
        ):
            result = svc.create_swap(
                db, from_agent_id=agent.id, from_currency="BTC", from_amount=Decimal("0.01")
            )

        # Should still succeed — order created, no deposit_address
        assert result["swap_id"] is not None
        assert result["deposit_address"] is None

    def test_create_swap_succeeds_when_hub_address_not_configured(self, db):
        """create_swap() gracefully handles missing XMR_HUB_ADDRESS."""
        from unittest.mock import patch
        from sthrip.services.swap_service import SwapService

        agent = _make_agent(db)
        db.commit()

        svc = SwapService()
        with patch.dict("os.environ", {}, clear=False):
            # Remove XMR_HUB_ADDRESS if present
            import os
            os.environ.pop("XMR_HUB_ADDRESS", None)
            result = svc.create_swap(
                db, from_agent_id=agent.id, from_currency="BTC", from_amount=Decimal("0.01")
            )

        assert result["swap_id"] is not None
        # No deposit_address — exchange was not contacted
        assert result["deposit_address"] is None

    def test_get_pending_external_orders_delegates_to_repo(self, db):
        """get_pending_external_orders() returns orders with external_order_id set."""
        from sthrip.services.swap_service import SwapService

        agent = _make_agent(db)
        _, htlc_hash = _valid_htlc_pair()
        lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=agent.id,
            from_currency="BTC",
            from_amount=Decimal("0.01"),
            to_currency="XMR",
            to_amount=Decimal("1.5"),
            exchange_rate=Decimal("150.0"),
            fee_amount=Decimal("0.015"),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        db.commit()
        repo.set_external_order(
            swap_id=order.id,
            external_order_id="ext-pending",
            deposit_address="bc1q...",
            provider_name="changenow",
        )
        db.commit()

        svc = SwapService()
        pending = svc.get_pending_external_orders(db)
        assert any(o.id == order.id for o in pending)

    def test_create_swap_response_includes_new_schema_fields(self, db):
        """create_swap result dict always includes deposit_address/external_order_id/provider_name keys."""
        from unittest.mock import patch
        from sthrip.services.swap_service import SwapService

        agent = _make_agent(db)
        db.commit()

        svc = SwapService()
        with patch(
            "sthrip.services.swap_service.create_order_with_fallback",
            side_effect=RuntimeError("not configured"),
        ):
            result = svc.create_swap(
                db, from_agent_id=agent.id, from_currency="BTC", from_amount=Decimal("0.01")
            )

        assert "deposit_address" in result
        assert "external_order_id" in result
        assert "provider_name" in result
