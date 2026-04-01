"""
Sprint 5: Atomic Multi-Party Payments -- service + API integration tests.

Tests cover:
  - create_multi_party (funds locked, recipients created)
  - all_accept with require_all_accept=True -> COMPLETED, funds distributed
  - one reject with require_all_accept=True -> REJECTED, all refunded
  - partial accept with require_all_accept=False -> individual distribution
  - sender cannot be a recipient
  - duplicate recipients rejected
  - non-participant cannot accept (403)
  - double-accept is idempotent
  - expiry refunds stale payments
  - API integration for each endpoint
"""

import os
import contextlib
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
    EscrowDeal, EscrowMilestone,
    MultiPartyPayment, MultiPartyRecipient,
    SpendingPolicy, WebhookEndpoint, MessageRelay,
    MultisigEscrow, MultisigRound,
    SLATemplate, SLAContract,
    AgentReview, AgentRatingSummary,
    MatchRequest, RecurringPayment,
    PaymentChannel, ChannelUpdate,
    PaymentStream, CurrencyConversion, SwapOrder,
    TreasuryPolicy, TreasuryForecast, TreasuryRebalanceLog,
    AgentCreditScore, AgentLoan, LendingOffer,
    ConditionalPayment,
)
from sthrip.db.enums import MultiPartyPaymentState
from sthrip.db.repository import (
    AgentRepository, BalanceRepository, MultiPartyRepository,
)
from sthrip.services.multi_party_service import MultiPartyService


# Valid 95-char stagenet XMR address (base58 alphabet, starts with '5')
_VALID_XMR_ADDR = "5" + "a" * 94


# ── Tables needed for multi-party tests ──────────────────────────────────

_MP_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    PendingWithdrawal.__table__,
    Transaction.__table__,
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
    CurrencyConversion.__table__,
    SwapOrder.__table__,
    SpendingPolicy.__table__,
    WebhookEndpoint.__table__,
    MessageRelay.__table__,
    TreasuryPolicy.__table__,
    TreasuryForecast.__table__,
    TreasuryRebalanceLog.__table__,
    AgentCreditScore.__table__,
    AgentLoan.__table__,
    LendingOffer.__table__,
    ConditionalPayment.__table__,
    MultiPartyPayment.__table__,
    MultiPartyRecipient.__table__,
]


# Modules where get_db must be patched (includes multi_party router).
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
    "api.routers.spending_policy",
    "api.routers.webhook_endpoints",
    "api.routers.reputation",
    "api.routers.messages",
    "api.routers.multisig_escrow",
    "api.routers.sla",
    "api.routers.reviews",
    "api.routers.matchmaking",
    "api.routers.channels",
    "api.routers.subscriptions",
    "api.routers.streams",
    "api.routers.conversion",
    "api.routers.swap",
    "api.routers.lending",
    "api.routers.treasury",
    "api.routers.multi_party",
    "api.routers.conditional_payments",
    "api.routers.split_payments",
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
# SQLite timezone compatibility helper
# ---------------------------------------------------------------------------

def _naive_utc_now() -> datetime:
    """Return current UTC time as a naive datetime (no tzinfo)."""
    return datetime.utcnow()


# ═══════════════════════════════════════════════════════════════════════════
# UNIT TEST FIXTURES (direct service calls, no HTTP)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mp_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_MP_TEST_TABLES)
    return engine


@pytest.fixture
def mp_session_factory(mp_engine):
    return sessionmaker(bind=mp_engine, expire_on_commit=False)


@pytest.fixture
def mp_session(mp_session_factory):
    session = mp_session_factory()
    yield session
    session.close()


@pytest.fixture
def three_agents(mp_session):
    """Create sender + 2 recipients, each with balance. Returns (sender, r1, r2) agents."""
    agents = []
    for name in ["sender-agent", "recipient-one", "recipient-two"]:
        agent = Agent(id=uuid.uuid4(), agent_name=name, is_active=True)
        mp_session.add(agent)
        mp_session.flush()
        # Give each agent some balance
        bal = AgentBalance(agent_id=agent.id, token="XMR", available=Decimal("10.0"))
        mp_session.add(bal)
        agents.append(agent)
    mp_session.flush()
    return agents


@pytest.fixture
def svc():
    return MultiPartyService()


# ═══════════════════════════════════════════════════════════════════════════
# UNIT TESTS: MultiPartyService
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateMultiParty:
    """Test create_multi_party service method."""

    def test_create_multi_party(self, mp_session, three_agents, svc):
        """Happy path: funds locked, recipients created, PENDING state."""
        sender, r1, r2 = three_agents
        with patch("sthrip.services.multi_party_service.audit_log"):
            with patch("sthrip.services.multi_party_service.queue_webhook"):
                result = svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r1.agent_name, "amount": Decimal("1.5")},
                        {"agent_name": r2.agent_name, "amount": Decimal("2.5")},
                    ],
                )

        assert result["state"] == "pending"
        assert result["total_amount"] == "4.0"
        assert len(result["recipients"]) == 2

        # Verify sender balance deducted
        bal = BalanceRepository(mp_session).get_available(sender.id, "XMR")
        assert bal == Decimal("6.0")  # 10 - 4 = 6

    def test_sender_not_recipient(self, mp_session, three_agents, svc):
        """Sender cannot be in the recipient list."""
        sender, r1, _r2 = three_agents
        with pytest.raises(ValueError, match="Sender cannot be a recipient"):
            with patch("sthrip.services.multi_party_service.audit_log"):
                with patch("sthrip.services.multi_party_service.queue_webhook"):
                    svc.create_multi_party(
                        db=mp_session,
                        sender_id=sender.id,
                        recipients=[
                            {"agent_name": sender.agent_name, "amount": Decimal("1.0")},
                            {"agent_name": r1.agent_name, "amount": Decimal("1.0")},
                        ],
                    )

    def test_duplicate_recipients_rejected(self, mp_session, three_agents, svc):
        """Duplicate agent_name in recipients list is rejected."""
        sender, r1, _r2 = three_agents
        with pytest.raises(ValueError, match="Duplicate recipients"):
            with patch("sthrip.services.multi_party_service.audit_log"):
                with patch("sthrip.services.multi_party_service.queue_webhook"):
                    svc.create_multi_party(
                        db=mp_session,
                        sender_id=sender.id,
                        recipients=[
                            {"agent_name": r1.agent_name, "amount": Decimal("1.0")},
                            {"agent_name": r1.agent_name, "amount": Decimal("2.0")},
                        ],
                    )

    def test_empty_recipients_rejected(self, mp_session, three_agents, svc):
        """At least 1 recipient required."""
        sender, _r1, _r2 = three_agents
        with pytest.raises(ValueError, match="At least 1 recipient"):
            with patch("sthrip.services.multi_party_service.audit_log"):
                with patch("sthrip.services.multi_party_service.queue_webhook"):
                    svc.create_multi_party(
                        db=mp_session,
                        sender_id=sender.id,
                        recipients=[],
                    )

    def test_unknown_recipient_rejected(self, mp_session, three_agents, svc):
        """Unknown agent_name raises LookupError."""
        sender, _r1, _r2 = three_agents
        with pytest.raises(LookupError, match="not found"):
            with patch("sthrip.services.multi_party_service.audit_log"):
                with patch("sthrip.services.multi_party_service.queue_webhook"):
                    svc.create_multi_party(
                        db=mp_session,
                        sender_id=sender.id,
                        recipients=[
                            {"agent_name": "nonexistent-agent", "amount": Decimal("1.0")},
                        ],
                    )

    def test_insufficient_balance(self, mp_session, three_agents, svc):
        """Insufficient balance raises ValueError."""
        sender, r1, r2 = three_agents
        with pytest.raises(ValueError, match="Insufficient balance"):
            with patch("sthrip.services.multi_party_service.audit_log"):
                with patch("sthrip.services.multi_party_service.queue_webhook"):
                    svc.create_multi_party(
                        db=mp_session,
                        sender_id=sender.id,
                        recipients=[
                            {"agent_name": r1.agent_name, "amount": Decimal("6.0")},
                            {"agent_name": r2.agent_name, "amount": Decimal("6.0")},
                        ],
                    )

    def test_zero_amount_rejected(self, mp_session, three_agents, svc):
        """Zero or negative amount raises ValueError."""
        sender, r1, _r2 = three_agents
        with pytest.raises(ValueError, match="positive"):
            with patch("sthrip.services.multi_party_service.audit_log"):
                with patch("sthrip.services.multi_party_service.queue_webhook"):
                    svc.create_multi_party(
                        db=mp_session,
                        sender_id=sender.id,
                        recipients=[
                            {"agent_name": r1.agent_name, "amount": Decimal("0")},
                        ],
                    )


class TestAcceptRejectFlow:
    """Test accept/reject flows for require_all_accept=True and False."""

    def test_all_accept_require_all_true(self, mp_session, three_agents, svc):
        """When all recipients accept (require_all=True), state->COMPLETED, funds distributed."""
        sender, r1, r2 = three_agents
        with patch("sthrip.services.multi_party_service.audit_log"):
            with patch("sthrip.services.multi_party_service.queue_webhook"):
                result = svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r1.agent_name, "amount": Decimal("3.0")},
                        {"agent_name": r2.agent_name, "amount": Decimal("2.0")},
                    ],
                    require_all_accept=True,
                )
                payment_id = uuid.UUID(result["payment_id"])

                # r1 accepts
                status1 = svc.accept(db=mp_session, recipient_agent_id=r1.id, payment_id=payment_id)
                assert status1["state"] == "pending"  # Still pending, not all accepted

                # r2 accepts -> now all accepted -> COMPLETED
                status2 = svc.accept(db=mp_session, recipient_agent_id=r2.id, payment_id=payment_id)
                assert status2["state"] == "completed"

        mp_session.expire_all()

        # Verify balances: sender started with 10, paid 5, so 5 left
        sender_bal = BalanceRepository(mp_session).get_available(sender.id, "XMR")
        assert sender_bal == Decimal("5.0")

        # r1 gets 3.0: 10 + 3 = 13
        r1_bal = BalanceRepository(mp_session).get_available(r1.id, "XMR")
        assert r1_bal == Decimal("13.0")

        # r2 gets 2.0: 10 + 2 = 12
        r2_bal = BalanceRepository(mp_session).get_available(r2.id, "XMR")
        assert r2_bal == Decimal("12.0")

    def test_one_reject_require_all_true(self, mp_session, three_agents, svc):
        """When one recipient rejects (require_all=True), state->REJECTED, sender refunded."""
        sender, r1, r2 = three_agents
        with patch("sthrip.services.multi_party_service.audit_log"):
            with patch("sthrip.services.multi_party_service.queue_webhook"):
                result = svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r1.agent_name, "amount": Decimal("3.0")},
                        {"agent_name": r2.agent_name, "amount": Decimal("2.0")},
                    ],
                    require_all_accept=True,
                )
                payment_id = uuid.UUID(result["payment_id"])

                # r1 rejects -> whole payment REJECTED
                status = svc.reject(db=mp_session, recipient_agent_id=r1.id, payment_id=payment_id)
                assert status["state"] == "rejected"

        mp_session.expire_all()

        # Sender gets full refund: back to 10
        sender_bal = BalanceRepository(mp_session).get_available(sender.id, "XMR")
        assert sender_bal == Decimal("10.0")

        # Recipients unchanged
        r1_bal = BalanceRepository(mp_session).get_available(r1.id, "XMR")
        assert r1_bal == Decimal("10.0")
        r2_bal = BalanceRepository(mp_session).get_available(r2.id, "XMR")
        assert r2_bal == Decimal("10.0")

    def test_partial_accept_require_all_false(self, mp_session, three_agents, svc):
        """When require_all_accept=False, each acceptance distributes individually."""
        sender, r1, r2 = three_agents
        with patch("sthrip.services.multi_party_service.audit_log"):
            with patch("sthrip.services.multi_party_service.queue_webhook"):
                result = svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r1.agent_name, "amount": Decimal("3.0")},
                        {"agent_name": r2.agent_name, "amount": Decimal("2.0")},
                    ],
                    require_all_accept=False,
                )
                payment_id = uuid.UUID(result["payment_id"])

                # r1 accepts -> gets their portion immediately
                status1 = svc.accept(db=mp_session, recipient_agent_id=r1.id, payment_id=payment_id)
                assert status1["recipient_state"] == "accepted"

        # Expire cached ORM objects so we see fresh DB values
        mp_session.expire_all()

        # r1 gets 3.0 immediately: 10 + 3 = 13
        r1_bal = BalanceRepository(mp_session).get_available(r1.id, "XMR")
        assert r1_bal == Decimal("13.0")

        # r2 not yet accepted, no credit
        r2_bal = BalanceRepository(mp_session).get_available(r2.id, "XMR")
        assert r2_bal == Decimal("10.0")

    def test_partial_reject_require_all_false(self, mp_session, three_agents, svc):
        """When require_all_accept=False, rejection refunds only that recipient's portion."""
        sender, r1, r2 = three_agents
        with patch("sthrip.services.multi_party_service.audit_log"):
            with patch("sthrip.services.multi_party_service.queue_webhook"):
                result = svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r1.agent_name, "amount": Decimal("3.0")},
                        {"agent_name": r2.agent_name, "amount": Decimal("2.0")},
                    ],
                    require_all_accept=False,
                )
                payment_id = uuid.UUID(result["payment_id"])

                # r1 rejects -> only r1's portion refunded to sender
                status = svc.reject(db=mp_session, recipient_agent_id=r1.id, payment_id=payment_id)
                assert status["recipient_state"] == "rejected"

        mp_session.expire_all()

        # Sender gets r1's 3.0 back: 10 - 5 + 3 = 8
        sender_bal = BalanceRepository(mp_session).get_available(sender.id, "XMR")
        assert sender_bal == Decimal("8.0")

    def test_non_participant_cannot_accept(self, mp_session, three_agents, svc):
        """Non-participant trying to accept raises PermissionError."""
        sender, r1, r2 = three_agents
        # Create a 4th agent who is not a participant
        outsider = Agent(id=uuid.uuid4(), agent_name="outsider-agent", is_active=True)
        mp_session.add(outsider)
        mp_session.flush()

        with patch("sthrip.services.multi_party_service.audit_log"):
            with patch("sthrip.services.multi_party_service.queue_webhook"):
                result = svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r1.agent_name, "amount": Decimal("1.0")},
                    ],
                )
                payment_id = uuid.UUID(result["payment_id"])

                with pytest.raises(PermissionError, match="not a recipient"):
                    svc.accept(db=mp_session, recipient_agent_id=outsider.id, payment_id=payment_id)

    def test_double_accept_idempotent(self, mp_session, three_agents, svc):
        """Accepting twice is idempotent (no error, no double-credit)."""
        sender, r1, r2 = three_agents
        with patch("sthrip.services.multi_party_service.audit_log"):
            with patch("sthrip.services.multi_party_service.queue_webhook"):
                result = svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r1.agent_name, "amount": Decimal("2.0")},
                        {"agent_name": r2.agent_name, "amount": Decimal("1.0")},
                    ],
                    require_all_accept=True,
                )
                payment_id = uuid.UUID(result["payment_id"])

                # First accept
                svc.accept(db=mp_session, recipient_agent_id=r1.id, payment_id=payment_id)
                # Second accept -- idempotent
                status = svc.accept(db=mp_session, recipient_agent_id=r1.id, payment_id=payment_id)
                assert status["recipient_state"] == "already_accepted"

    def test_accept_wrong_state(self, mp_session, three_agents, svc):
        """Cannot accept on a non-PENDING payment."""
        sender, r1, r2 = three_agents
        with patch("sthrip.services.multi_party_service.audit_log"):
            with patch("sthrip.services.multi_party_service.queue_webhook"):
                result = svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r1.agent_name, "amount": Decimal("1.0")},
                    ],
                    require_all_accept=True,
                )
                payment_id = uuid.UUID(result["payment_id"])

                # Reject it first
                svc.reject(db=mp_session, recipient_agent_id=r1.id, payment_id=payment_id)

                # Now r1 tries to accept a rejected payment
                with pytest.raises(ValueError, match="not in PENDING state"):
                    svc.accept(db=mp_session, recipient_agent_id=r1.id, payment_id=payment_id)


class TestGetStatusAndList:
    """Test get_status and list_by_agent."""

    def test_get_status(self, mp_session, three_agents, svc):
        """Sender or recipient can get payment status."""
        sender, r1, r2 = three_agents
        with patch("sthrip.services.multi_party_service.audit_log"):
            with patch("sthrip.services.multi_party_service.queue_webhook"):
                result = svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r1.agent_name, "amount": Decimal("1.0")},
                        {"agent_name": r2.agent_name, "amount": Decimal("2.0")},
                    ],
                )
                payment_id = uuid.UUID(result["payment_id"])

                # Sender can see status
                status = svc.get_status(db=mp_session, payment_id=payment_id, agent_id=sender.id)
                assert status["payment_id"] == str(payment_id)
                assert status["state"] == "pending"
                assert len(status["recipients"]) == 2

                # Recipient can see status
                status_r1 = svc.get_status(db=mp_session, payment_id=payment_id, agent_id=r1.id)
                assert status_r1["payment_id"] == str(payment_id)

    def test_get_status_unauthorized(self, mp_session, three_agents, svc):
        """Non-participant cannot view status."""
        sender, r1, _r2 = three_agents
        outsider = Agent(id=uuid.uuid4(), agent_name="outsider-2", is_active=True)
        mp_session.add(outsider)
        mp_session.flush()

        with patch("sthrip.services.multi_party_service.audit_log"):
            with patch("sthrip.services.multi_party_service.queue_webhook"):
                result = svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r1.agent_name, "amount": Decimal("1.0")},
                    ],
                )
                payment_id = uuid.UUID(result["payment_id"])

                with pytest.raises(PermissionError, match="not authorized"):
                    svc.get_status(db=mp_session, payment_id=payment_id, agent_id=outsider.id)

    def test_list_by_agent(self, mp_session, three_agents, svc):
        """list_by_agent returns payments where agent is sender or recipient."""
        sender, r1, r2 = three_agents
        with patch("sthrip.services.multi_party_service.audit_log"):
            with patch("sthrip.services.multi_party_service.queue_webhook"):
                svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r1.agent_name, "amount": Decimal("1.0")},
                    ],
                )
                svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r2.agent_name, "amount": Decimal("1.0")},
                    ],
                )

                # Sender sees both
                items = svc.list_by_agent(db=mp_session, agent_id=sender.id)
                assert items["total"] == 2

                # r1 sees 1
                items_r1 = svc.list_by_agent(db=mp_session, agent_id=r1.id)
                assert items_r1["total"] == 1

                # r2 sees 1
                items_r2 = svc.list_by_agent(db=mp_session, agent_id=r2.id)
                assert items_r2["total"] == 1

                # Filter by role
                sender_only = svc.list_by_agent(db=mp_session, agent_id=sender.id, role="sender")
                assert sender_only["total"] == 2

                recipient_only = svc.list_by_agent(db=mp_session, agent_id=r1.id, role="recipient")
                assert recipient_only["total"] == 1


class TestExpiryRefunds:
    """Test expire_stale auto-expiry."""

    def test_expiry_refunds(self, mp_session, three_agents, svc):
        """Expired payments are refunded to sender."""
        sender, r1, r2 = three_agents
        with patch("sthrip.services.multi_party_service.audit_log"):
            with patch("sthrip.services.multi_party_service.queue_webhook"):
                result = svc.create_multi_party(
                    db=mp_session,
                    sender_id=sender.id,
                    recipients=[
                        {"agent_name": r1.agent_name, "amount": Decimal("3.0")},
                        {"agent_name": r2.agent_name, "amount": Decimal("2.0")},
                    ],
                    accept_hours=0,  # Expires immediately
                )
                mp_session.flush()

                # Manually set the accept_deadline in the past
                payment_id = uuid.UUID(result["payment_id"])
                payment = mp_session.query(MultiPartyPayment).filter_by(id=payment_id).first()
                payment.accept_deadline = datetime.utcnow() - timedelta(hours=1)
                mp_session.flush()

                expired_count = svc.expire_stale(db=mp_session)
                assert expired_count >= 1

        # Sender refunded: back to 10
        sender_bal = BalanceRepository(mp_session).get_available(sender.id, "XMR")
        assert sender_bal == Decimal("10.0")


# ═══════════════════════════════════════════════════════════════════════════
# API INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mp_client(mp_engine, mp_session_factory):
    """FastAPI test client with all deps mocked, including multi_party router."""

    @contextmanager
    def get_test_db():
        session = mp_session_factory()
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
            patch("sthrip.services.multi_party_service.audit_log")
        )
        stack.enter_context(
            patch("sthrip.services.multi_party_service.queue_webhook")
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
    return {"Authorization": f"Bearer {api_key}"}


class TestMultiPartyAPI:
    """API integration tests for multi-party payment endpoints."""

    def test_api_create_multi_party(self, mp_client):
        """POST /v2/payments/multi creates a multi-party payment."""
        sender_key = _register_agent(mp_client, "api-sender")
        _register_agent(mp_client, "api-recv-1")
        _register_agent(mp_client, "api-recv-2")
        _deposit(mp_client, sender_key, 10.0)

        r = mp_client.post(
            "/v2/payments/multi",
            json={
                "recipients": [
                    {"agent_name": "api-recv-1", "amount": 3.0},
                    {"agent_name": "api-recv-2", "amount": 2.0},
                ],
            },
            headers=_auth(sender_key),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["state"] == "pending"
        assert body["total_amount"] == "5.0"
        assert len(body["recipients"]) == 2

    def test_api_get_status(self, mp_client):
        """GET /v2/payments/multi/{id} returns payment status."""
        sender_key = _register_agent(mp_client, "status-sender")
        _register_agent(mp_client, "status-recv")
        _deposit(mp_client, sender_key, 10.0)

        create_r = mp_client.post(
            "/v2/payments/multi",
            json={
                "recipients": [
                    {"agent_name": "status-recv", "amount": 2.0},
                ],
            },
            headers=_auth(sender_key),
        )
        assert create_r.status_code == 201
        payment_id = create_r.json()["payment_id"]

        r = mp_client.get(
            f"/v2/payments/multi/{payment_id}",
            headers=_auth(sender_key),
        )
        assert r.status_code == 200
        assert r.json()["state"] == "pending"

    def test_api_accept(self, mp_client):
        """POST /v2/payments/multi/{id}/accept transitions correctly."""
        sender_key = _register_agent(mp_client, "acc-sender")
        recv_key = _register_agent(mp_client, "acc-recv")
        _deposit(mp_client, sender_key, 10.0)

        create_r = mp_client.post(
            "/v2/payments/multi",
            json={
                "recipients": [
                    {"agent_name": "acc-recv", "amount": 2.0},
                ],
                "require_all_accept": True,
            },
            headers=_auth(sender_key),
        )
        assert create_r.status_code == 201
        payment_id = create_r.json()["payment_id"]

        # Recipient accepts
        r = mp_client.post(
            f"/v2/payments/multi/{payment_id}/accept",
            headers=_auth(recv_key),
        )
        assert r.status_code == 200
        assert r.json()["state"] == "completed"

    def test_api_reject(self, mp_client):
        """POST /v2/payments/multi/{id}/reject transitions correctly."""
        sender_key = _register_agent(mp_client, "rej-sender")
        recv_key = _register_agent(mp_client, "rej-recv")
        _deposit(mp_client, sender_key, 10.0)

        create_r = mp_client.post(
            "/v2/payments/multi",
            json={
                "recipients": [
                    {"agent_name": "rej-recv", "amount": 2.0},
                ],
                "require_all_accept": True,
            },
            headers=_auth(sender_key),
        )
        assert create_r.status_code == 201
        payment_id = create_r.json()["payment_id"]

        r = mp_client.post(
            f"/v2/payments/multi/{payment_id}/reject",
            headers=_auth(recv_key),
        )
        assert r.status_code == 200
        assert r.json()["state"] == "rejected"

    def test_api_list(self, mp_client):
        """GET /v2/payments/multi lists payments for the authenticated agent."""
        sender_key = _register_agent(mp_client, "list-sender")
        _register_agent(mp_client, "list-recv")
        _deposit(mp_client, sender_key, 10.0)

        mp_client.post(
            "/v2/payments/multi",
            json={
                "recipients": [
                    {"agent_name": "list-recv", "amount": 1.0},
                ],
            },
            headers=_auth(sender_key),
        )

        r = mp_client.get(
            "/v2/payments/multi",
            headers=_auth(sender_key),
        )
        assert r.status_code == 200, f"List failed: {r.text}"
        body = r.json()
        assert body["total"] >= 1
        assert len(body["items"]) >= 1

    def test_api_non_participant_cannot_accept(self, mp_client):
        """Non-participant gets 403 when trying to accept."""
        sender_key = _register_agent(mp_client, "np-sender")
        _register_agent(mp_client, "np-recv")
        outsider_key = _register_agent(mp_client, "np-outsider")
        _deposit(mp_client, sender_key, 10.0)

        create_r = mp_client.post(
            "/v2/payments/multi",
            json={
                "recipients": [
                    {"agent_name": "np-recv", "amount": 2.0},
                ],
            },
            headers=_auth(sender_key),
        )
        assert create_r.status_code == 201
        payment_id = create_r.json()["payment_id"]

        r = mp_client.post(
            f"/v2/payments/multi/{payment_id}/accept",
            headers=_auth(outsider_key),
        )
        assert r.status_code == 403

    def test_api_sender_not_recipient(self, mp_client):
        """Sender in recipients list returns 400."""
        sender_key = _register_agent(mp_client, "self-sender")
        _deposit(mp_client, sender_key, 10.0)

        r = mp_client.post(
            "/v2/payments/multi",
            json={
                "recipients": [
                    {"agent_name": "self-sender", "amount": 2.0},
                ],
            },
            headers=_auth(sender_key),
        )
        assert r.status_code == 400
        assert "recipient" in r.json()["detail"].lower()

    def test_api_duplicate_recipients(self, mp_client):
        """Duplicate recipients returns 422 (Pydantic validation) or 400."""
        sender_key = _register_agent(mp_client, "dup-sender")
        _register_agent(mp_client, "dup-recv")
        _deposit(mp_client, sender_key, 10.0)

        r = mp_client.post(
            "/v2/payments/multi",
            json={
                "recipients": [
                    {"agent_name": "dup-recv", "amount": 1.0},
                    {"agent_name": "dup-recv", "amount": 2.0},
                ],
            },
            headers=_auth(sender_key),
        )
        assert r.status_code in (400, 422)

    def test_api_not_found(self, mp_client):
        """Accessing nonexistent payment returns 404."""
        sender_key = _register_agent(mp_client, "nf-sender")
        fake_id = str(uuid.uuid4())

        r = mp_client.get(
            f"/v2/payments/multi/{fake_id}",
            headers=_auth(sender_key),
        )
        assert r.status_code == 404
