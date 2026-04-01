"""
Tests for Payment Streams feature.

TDD: tests written first (RED), then implementation (GREEN).

Unit tests cover:
- PaymentStreamRepository CRUD and state transitions
- StreamService business logic (start, accrue, pause, resume, stop)

API tests cover:
- POST /v2/streams  (start stream)
- GET  /v2/streams/{id}  (get stream + accrued)
- POST /v2/streams/{id}/pause
- POST /v2/streams/{id}/resume
- POST /v2/streams/{id}/stop
"""

import os
import contextlib
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock

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
    MatchRequest,
    RecurringPayment,
    PaymentChannel, ChannelUpdate,
    PaymentStream,
    ChannelStatus, StreamStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TABLES = [
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
    "api.routers.matchmaking",
    "api.routers.channels",
    "api.routers.subscriptions",
    "api.routers.streams",
]

_TEST_ENCRYPTION_KEY = "uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE="


def _make_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_TABLES)
    return engine


def _make_agent(db, name: str = "test-agent") -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=name,
        api_key_hash="testhash",
        is_active=True,
    )
    db.add(agent)
    db.flush()
    return agent


def _make_channel(db, agent_a: Agent, agent_b: Agent, balance_a: Decimal = Decimal("1.0")) -> PaymentChannel:
    ch = PaymentChannel(
        id=uuid.uuid4(),
        channel_hash=f"hash-{uuid.uuid4().hex}",
        agent_a_id=agent_a.id,
        agent_b_id=agent_b.id,
        capacity=balance_a + Decimal("1.0"),
        deposit_a=balance_a,
        deposit_b=Decimal("1.0"),
        balance_a=balance_a,
        balance_b=Decimal("1.0"),
        nonce=0,
        status=ChannelStatus.OPEN,
        current_state={},
        settlement_period=3600,
    )
    db.add(ch)
    db.flush()
    return ch


# ===========================================================================
# UNIT TESTS — PaymentStreamRepository
# ===========================================================================

class TestPaymentStreamRepository:
    """Unit tests for PaymentStreamRepository."""

    def setup_method(self):
        self.engine = _make_engine()
        Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = Session()
        self.agent_a = _make_agent(self.db, "agent-a")
        self.agent_b = _make_agent(self.db, "agent-b")
        self.channel = _make_channel(self.db, self.agent_a, self.agent_b)
        self.db.commit()

    def teardown_method(self):
        self.db.close()
        self.engine.dispose()

    def test_create_stream(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        stream = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        assert stream.id is not None
        assert stream.channel_id == self.channel.id
        assert stream.from_agent_id == self.agent_a.id
        assert stream.to_agent_id == self.agent_b.id
        assert Decimal(str(stream.rate_per_second)) == Decimal("0.001")
        assert stream.state == StreamStatus.ACTIVE
        assert stream.paused_at is None
        assert stream.stopped_at is None

    def test_get_by_id(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        stream = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        self.db.commit()

        fetched = repo.get_by_id(stream.id)
        assert fetched is not None
        assert fetched.id == stream.id

    def test_get_by_id_missing_returns_none(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        result = repo.get_by_id(uuid.uuid4())
        assert result is None

    def test_get_by_channel_returns_active(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        s1 = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        # Also create a stopped stream on same channel
        s2 = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.002"),
        )
        repo.stop(s2.id, total_streamed=Decimal("0"))
        self.db.commit()

        active = repo.get_by_channel(self.channel.id)
        ids = [s.id for s in active]
        assert s1.id in ids
        assert s2.id not in ids

    def test_pause_stream(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        stream = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        self.db.commit()

        rows = repo.pause(stream.id)
        self.db.commit()
        assert rows == 1

        updated = repo.get_by_id(stream.id)
        assert updated.state == StreamStatus.PAUSED
        assert updated.paused_at is not None

    def test_pause_non_active_stream_returns_zero(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        stream = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        repo.pause(stream.id)
        self.db.commit()

        # Pausing an already-paused stream should return 0
        rows = repo.pause(stream.id)
        self.db.commit()
        assert rows == 0

    def test_resume_stream(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        stream = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        repo.pause(stream.id)
        self.db.commit()

        rows = repo.resume(stream.id)
        self.db.commit()
        assert rows == 1

        updated = repo.get_by_id(stream.id)
        assert updated.state == StreamStatus.ACTIVE
        assert updated.paused_at is None

    def test_resume_non_paused_returns_zero(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        stream = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        self.db.commit()

        # Active stream cannot be resumed
        rows = repo.resume(stream.id)
        self.db.commit()
        assert rows == 0

    def test_stop_stream(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        stream = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        self.db.commit()

        rows = repo.stop(stream.id, total_streamed=Decimal("0.05"))
        self.db.commit()
        assert rows == 1

        updated = repo.get_by_id(stream.id)
        assert updated.state == StreamStatus.STOPPED
        assert updated.stopped_at is not None
        assert Decimal(str(updated.total_streamed)) == Decimal("0.05")

    def test_stop_paused_stream(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        stream = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        repo.pause(stream.id)
        self.db.commit()

        # Stop from PAUSED state should also work
        rows = repo.stop(stream.id, total_streamed=Decimal("0.02"))
        self.db.commit()
        assert rows == 1

        updated = repo.get_by_id(stream.id)
        assert updated.state == StreamStatus.STOPPED

    def test_list_by_agent(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.002"),
        )
        self.db.commit()

        items, total = repo.list_by_agent(self.agent_a.id, limit=50, offset=0)
        assert total == 2
        assert len(items) == 2

    def test_list_by_agent_pagination(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        for i in range(5):
            repo.create(
                channel_id=self.channel.id,
                from_agent_id=self.agent_a.id,
                to_agent_id=self.agent_b.id,
                rate_per_second=Decimal("0.001"),
            )
        self.db.commit()

        items, total = repo.list_by_agent(self.agent_a.id, limit=2, offset=0)
        assert total == 5
        assert len(items) == 2

    def test_list_by_agent_as_recipient(self):
        from sthrip.db.stream_repo import PaymentStreamRepository
        repo = PaymentStreamRepository(self.db)
        repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        self.db.commit()

        # agent_b is the recipient — should appear in list
        items, total = repo.list_by_agent(self.agent_b.id, limit=50, offset=0)
        assert total == 1


# ===========================================================================
# UNIT TESTS — StreamService
# ===========================================================================

class TestStreamService:
    """Unit tests for StreamService business logic."""

    def setup_method(self):
        self.engine = _make_engine()
        Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = Session()
        self.agent_a = _make_agent(self.db, "svc-agent-a")
        self.agent_b = _make_agent(self.db, "svc-agent-b")
        # Channel with 1.0 balance_a — supports 60 s at rate 0.001/s
        self.channel = _make_channel(self.db, self.agent_a, self.agent_b, balance_a=Decimal("1.0"))
        self.db.commit()

    def teardown_method(self):
        self.db.close()
        self.engine.dispose()

    # --- start_stream ---

    def test_start_stream(self):
        from sthrip.services.stream_service import StreamService
        svc = StreamService()
        result = svc.start_stream(
            db=self.db,
            channel_id=str(self.channel.id),
            from_agent_id=str(self.agent_a.id),
            rate_per_second=Decimal("0.001"),
        )
        assert result["stream_id"] is not None
        assert result["state"] == "active"
        assert result["rate_per_second"] == "0.001"
        assert result["channel_id"] == str(self.channel.id)

    def test_start_stream_no_channel_raises_lookup_error(self):
        from sthrip.services.stream_service import StreamService
        svc = StreamService()
        with pytest.raises(LookupError, match="Channel"):
            svc.start_stream(
                db=self.db,
                channel_id=str(uuid.uuid4()),
                from_agent_id=str(self.agent_a.id),
                rate_per_second=Decimal("0.001"),
            )

    def test_start_stream_closed_channel_raises_value_error(self):
        from sthrip.services.stream_service import StreamService
        self.channel.status = ChannelStatus.CLOSED
        self.db.commit()

        svc = StreamService()
        with pytest.raises(ValueError, match="(?i)open"):
            svc.start_stream(
                db=self.db,
                channel_id=str(self.channel.id),
                from_agent_id=str(self.agent_a.id),
                rate_per_second=Decimal("0.001"),
            )

    def test_start_stream_wrong_agent_raises_permission_error(self):
        from sthrip.services.stream_service import StreamService
        svc = StreamService()
        # agent_b is not agent_a of the channel
        with pytest.raises(PermissionError):
            svc.start_stream(
                db=self.db,
                channel_id=str(self.channel.id),
                from_agent_id=str(self.agent_b.id),
                rate_per_second=Decimal("0.001"),
            )

    def test_start_stream_rate_exceeds_balance_raises_value_error(self):
        from sthrip.services.stream_service import StreamService
        svc = StreamService()
        # rate=0.1/s needs 0.1 * 60 = 6.0 but balance_a = 1.0
        with pytest.raises(ValueError, match="[Rr]ate|[Bb]alance|[Ss]ustain"):
            svc.start_stream(
                db=self.db,
                channel_id=str(self.channel.id),
                from_agent_id=str(self.agent_a.id),
                rate_per_second=Decimal("0.1"),
            )

    # --- get_accrued ---

    def test_get_accrued_active_stream(self):
        from sthrip.services.stream_service import StreamService
        from sthrip.db.stream_repo import PaymentStreamRepository

        repo = PaymentStreamRepository(self.db)
        stream = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        # Backdate started_at by 100 seconds to simulate elapsed time
        stream.started_at = datetime.now(timezone.utc) - timedelta(seconds=100)
        self.db.commit()

        svc = StreamService()
        result = svc.get_accrued(self.db, str(stream.id))
        accrued = Decimal(result["accrued"])
        # rate=0.001, elapsed~100s => accrued ~ 0.1
        assert accrued >= Decimal("0.09")
        assert accrued <= Decimal("0.11")

    def test_get_accrued_paused_stream_uses_paused_at(self):
        from sthrip.services.stream_service import StreamService
        from sthrip.db.stream_repo import PaymentStreamRepository

        repo = PaymentStreamRepository(self.db)
        stream = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        # started 200s ago, paused 100s ago => elapsed=100s
        now = datetime.now(timezone.utc)
        stream.started_at = now - timedelta(seconds=200)
        stream.paused_at = now - timedelta(seconds=100)
        stream.state = StreamStatus.PAUSED
        self.db.commit()

        svc = StreamService()
        result = svc.get_accrued(self.db, str(stream.id))
        accrued = Decimal(result["accrued"])
        # rate=0.001, elapsed=100s => accrued = 0.1
        assert accrued == Decimal("0.1")

    def test_get_accrued_stopped_stream_uses_total_streamed(self):
        from sthrip.services.stream_service import StreamService
        from sthrip.db.stream_repo import PaymentStreamRepository

        repo = PaymentStreamRepository(self.db)
        stream = repo.create(
            channel_id=self.channel.id,
            from_agent_id=self.agent_a.id,
            to_agent_id=self.agent_b.id,
            rate_per_second=Decimal("0.001"),
        )
        stream.state = StreamStatus.STOPPED
        stream.total_streamed = Decimal("0.42")
        self.db.commit()

        svc = StreamService()
        result = svc.get_accrued(self.db, str(stream.id))
        assert Decimal(result["accrued"]) == Decimal("0.42")

    def test_get_accrued_missing_stream_raises_lookup_error(self):
        from sthrip.services.stream_service import StreamService
        svc = StreamService()
        with pytest.raises(LookupError):
            svc.get_accrued(self.db, str(uuid.uuid4()))

    # --- pause_stream ---

    def test_pause_stream(self):
        from sthrip.services.stream_service import StreamService

        svc = StreamService()
        result_start = svc.start_stream(
            db=self.db,
            channel_id=str(self.channel.id),
            from_agent_id=str(self.agent_a.id),
            rate_per_second=Decimal("0.001"),
        )
        stream_id = result_start["stream_id"]

        result_pause = svc.pause_stream(self.db, stream_id, str(self.agent_a.id))
        assert result_pause["state"] == "paused"

    def test_pause_stream_wrong_agent_raises_permission_error(self):
        from sthrip.services.stream_service import StreamService

        svc = StreamService()
        result_start = svc.start_stream(
            db=self.db,
            channel_id=str(self.channel.id),
            from_agent_id=str(self.agent_a.id),
            rate_per_second=Decimal("0.001"),
        )
        stream_id = result_start["stream_id"]

        # Third party agent cannot pause
        other = _make_agent(self.db, "other-agent")
        self.db.commit()
        with pytest.raises(PermissionError):
            svc.pause_stream(self.db, stream_id, str(other.id))

    def test_pause_stream_missing_raises_lookup_error(self):
        from sthrip.services.stream_service import StreamService
        svc = StreamService()
        with pytest.raises(LookupError):
            svc.pause_stream(self.db, str(uuid.uuid4()), str(self.agent_a.id))

    # --- resume_stream ---

    def test_resume_stream(self):
        from sthrip.services.stream_service import StreamService

        svc = StreamService()
        result_start = svc.start_stream(
            db=self.db,
            channel_id=str(self.channel.id),
            from_agent_id=str(self.agent_a.id),
            rate_per_second=Decimal("0.001"),
        )
        stream_id = result_start["stream_id"]

        svc.pause_stream(self.db, stream_id, str(self.agent_a.id))
        result_resume = svc.resume_stream(self.db, stream_id, str(self.agent_a.id))
        assert result_resume["state"] == "active"

    def test_resume_stream_wrong_agent_raises_permission_error(self):
        from sthrip.services.stream_service import StreamService

        svc = StreamService()
        result_start = svc.start_stream(
            db=self.db,
            channel_id=str(self.channel.id),
            from_agent_id=str(self.agent_a.id),
            rate_per_second=Decimal("0.001"),
        )
        stream_id = result_start["stream_id"]
        svc.pause_stream(self.db, stream_id, str(self.agent_a.id))

        other = _make_agent(self.db, "other-resume-agent")
        self.db.commit()
        with pytest.raises(PermissionError):
            svc.resume_stream(self.db, stream_id, str(other.id))

    # --- stop_stream ---

    def test_stop_stream(self):
        from sthrip.services.stream_service import StreamService
        from sthrip.db.stream_repo import PaymentStreamRepository

        svc = StreamService()
        result_start = svc.start_stream(
            db=self.db,
            channel_id=str(self.channel.id),
            from_agent_id=str(self.agent_a.id),
            rate_per_second=Decimal("0.001"),
        )
        stream_id = result_start["stream_id"]

        # Backdate started_at so there's meaningful accrual
        repo = PaymentStreamRepository(self.db)
        stream = repo.get_by_id(uuid.UUID(stream_id))
        stream.started_at = datetime.now(timezone.utc) - timedelta(seconds=50)
        self.db.commit()

        result_stop = svc.stop_stream(self.db, stream_id, str(self.agent_a.id))
        assert result_stop["state"] == "stopped"
        accrued = Decimal(result_stop["total_streamed"])
        assert accrued > Decimal("0")

    def test_stop_stream_wrong_agent_raises_permission_error(self):
        from sthrip.services.stream_service import StreamService

        svc = StreamService()
        result_start = svc.start_stream(
            db=self.db,
            channel_id=str(self.channel.id),
            from_agent_id=str(self.agent_a.id),
            rate_per_second=Decimal("0.001"),
        )
        stream_id = result_start["stream_id"]

        other = _make_agent(self.db, "other-stop-agent")
        self.db.commit()
        with pytest.raises(PermissionError):
            svc.stop_stream(self.db, stream_id, str(other.id))

    def test_stop_stream_missing_raises_lookup_error(self):
        from sthrip.services.stream_service import StreamService
        svc = StreamService()
        with pytest.raises(LookupError):
            svc.stop_stream(self.db, str(uuid.uuid4()), str(self.agent_a.id))

    def test_stop_already_stopped_raises_value_error(self):
        from sthrip.services.stream_service import StreamService

        svc = StreamService()
        result_start = svc.start_stream(
            db=self.db,
            channel_id=str(self.channel.id),
            from_agent_id=str(self.agent_a.id),
            rate_per_second=Decimal("0.001"),
        )
        stream_id = result_start["stream_id"]
        svc.stop_stream(self.db, stream_id, str(self.agent_a.id))

        with pytest.raises(ValueError, match="[Ss]topped|[Aa]lready"):
            svc.stop_stream(self.db, stream_id, str(self.agent_a.id))


# ===========================================================================
# API TESTS
# ===========================================================================

@pytest.fixture
def stream_client(monkeypatch):
    """FastAPI test client with streams router registered and get_db patched."""
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key-for-tests-long-enough-32")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)

    from sthrip.config import get_settings
    get_settings.cache_clear()
    import sthrip.crypto as _crypto
    _crypto._fernet_instance = None

    engine = _make_engine()
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    @contextlib.contextmanager
    def get_test_db():
        session = Session()
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
        "timestamp": "2026-03-03T00:00:00",
        "checks": {},
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

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
        client = TestClient(app, raise_server_exceptions=False)

        # Create agents and a channel directly in the DB for API tests
        with get_test_db() as db:
            agent_a = _make_agent(db, "api-agent-a")
            agent_b = _make_agent(db, "api-agent-b")
            channel = _make_channel(db, agent_a, agent_b, balance_a=Decimal("1.0"))

        yield {
            "client": client,
            "agent_a": agent_a,
            "agent_b": agent_b,
            "channel": channel,
            "get_test_db": get_test_db,
        }

    get_settings.cache_clear()
    import sthrip.crypto as _crypto
    _crypto._fernet_instance = None


def _override_auth(app, agent: Agent):
    """Apply a FastAPI dependency override so all requests are authenticated as agent."""
    from api.deps import get_current_agent

    async def _fake_auth():
        return agent

    app.dependency_overrides[get_current_agent] = _fake_auth
    return app


def _clear_auth(app):
    """Remove auth dependency overrides."""
    from api.deps import get_current_agent
    app.dependency_overrides.pop(get_current_agent, None)


class TestStreamsAPI:
    """API integration tests for /v2/streams endpoints."""

    def setup_method(self):
        """Reset dependency overrides before each test."""
        from api.main_v2 import app
        self._app = app

    def teardown_method(self):
        """Clear dependency overrides after each test."""
        _clear_auth(self._app)

    def _auth_as(self, agent: Agent) -> None:
        """Install a dependency override that returns agent for all requests."""
        _override_auth(self._app, agent)

    def test_api_start_stream(self, stream_client):
        client = stream_client["client"]
        agent_a = stream_client["agent_a"]
        channel = stream_client["channel"]

        self._auth_as(agent_a)
        resp = client.post(
            "/v2/streams",
            json={
                "channel_id": str(channel.id),
                "rate_per_second": "0.001",
            },
        )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "stream_id" in data
        assert data["state"] == "active"
        assert data["rate_per_second"] == "0.001"

    def test_api_start_stream_missing_channel_returns_404(self, stream_client):
        client = stream_client["client"]
        agent_a = stream_client["agent_a"]

        self._auth_as(agent_a)
        resp = client.post(
            "/v2/streams",
            json={
                "channel_id": str(uuid.uuid4()),
                "rate_per_second": "0.001",
            },
        )

        assert resp.status_code == 404

    def test_api_start_stream_rate_exceeds_balance_returns_400(self, stream_client):
        client = stream_client["client"]
        agent_a = stream_client["agent_a"]
        channel = stream_client["channel"]

        # rate=0.1/s needs 6.0 for 60s but balance_a=1.0
        self._auth_as(agent_a)
        resp = client.post(
            "/v2/streams",
            json={
                "channel_id": str(channel.id),
                "rate_per_second": "0.1",
            },
        )

        assert resp.status_code == 400

    def test_api_get_stream(self, stream_client):
        client = stream_client["client"]
        agent_a = stream_client["agent_a"]
        channel = stream_client["channel"]

        self._auth_as(agent_a)
        start_resp = client.post(
            "/v2/streams",
            json={"channel_id": str(channel.id), "rate_per_second": "0.001"},
        )
        assert start_resp.status_code == 201
        stream_id = start_resp.json()["stream_id"]

        resp = client.get(f"/v2/streams/{stream_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["stream_id"] == stream_id
        assert "accrued" in data

    def test_api_get_stream_not_found_returns_404(self, stream_client):
        client = stream_client["client"]
        agent_a = stream_client["agent_a"]

        self._auth_as(agent_a)
        resp = client.get(f"/v2/streams/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_api_pause_stream(self, stream_client):
        client = stream_client["client"]
        agent_a = stream_client["agent_a"]
        channel = stream_client["channel"]

        self._auth_as(agent_a)
        start_resp = client.post(
            "/v2/streams",
            json={"channel_id": str(channel.id), "rate_per_second": "0.001"},
        )
        stream_id = start_resp.json()["stream_id"]

        resp = client.post(f"/v2/streams/{stream_id}/pause")
        assert resp.status_code == 200
        assert resp.json()["state"] == "paused"

    def test_api_resume_stream(self, stream_client):
        client = stream_client["client"]
        agent_a = stream_client["agent_a"]
        channel = stream_client["channel"]

        self._auth_as(agent_a)
        start_resp = client.post(
            "/v2/streams",
            json={"channel_id": str(channel.id), "rate_per_second": "0.001"},
        )
        stream_id = start_resp.json()["stream_id"]

        client.post(f"/v2/streams/{stream_id}/pause")
        resp = client.post(f"/v2/streams/{stream_id}/resume")
        assert resp.status_code == 200
        assert resp.json()["state"] == "active"

    def test_api_stop_stream(self, stream_client):
        client = stream_client["client"]
        agent_a = stream_client["agent_a"]
        channel = stream_client["channel"]

        self._auth_as(agent_a)
        start_resp = client.post(
            "/v2/streams",
            json={"channel_id": str(channel.id), "rate_per_second": "0.001"},
        )
        stream_id = start_resp.json()["stream_id"]

        resp = client.post(f"/v2/streams/{stream_id}/stop")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "stopped"
        assert "total_streamed" in data

    def test_api_stop_already_stopped_returns_400(self, stream_client):
        client = stream_client["client"]
        agent_a = stream_client["agent_a"]
        channel = stream_client["channel"]

        self._auth_as(agent_a)
        start_resp = client.post(
            "/v2/streams",
            json={"channel_id": str(channel.id), "rate_per_second": "0.001"},
        )
        stream_id = start_resp.json()["stream_id"]

        client.post(f"/v2/streams/{stream_id}/stop")

        resp = client.post(f"/v2/streams/{stream_id}/stop")
        assert resp.status_code == 400
