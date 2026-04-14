"""
Tests for the swap background poller — poll_external_orders() and
SwapRepository.complete_from_external().

All exchange provider HTTP calls are mocked.  Tests use SQLite in-memory.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base,
    Agent,
    AgentReputation,
    AgentBalance,
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
    AgentRatingSummary,
    MatchRequest,
    RecurringPayment,
    PaymentChannel,
    ChannelUpdate,
    PaymentStream,
    SwapOrder,
    SwapStatus,
)
from sthrip.db.swap_repo import SwapRepository
from sthrip.db.balance_repo import BalanceRepository
from sthrip.services.swap_service import SwapService
from sthrip.services.exchange_providers import (
    ExchangeProviderError,
    STATUS_FINISHED,
    STATUS_FAILED,
    STATUS_EXPIRED,
    STATUS_WAITING,
    STATUS_CONFIRMING,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_POLLER_TABLES = [
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
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=_POLLER_TABLES)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _make_agent(db) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=f"agent-{uuid.uuid4().hex[:8]}",
        api_key_hash="testhash",
        is_active=True,
    )
    db.add(agent)
    db.flush()
    return agent


def _make_swap_order(db, agent_id, external_order_id=None, provider_name="changenow") -> SwapOrder:
    """Helper to create a SwapOrder in CREATED state with optional external fields."""
    import secrets as _secrets
    import hashlib

    secret = _secrets.token_hex(32)
    htlc_hash = hashlib.sha256(bytes.fromhex(secret)).hexdigest()
    lock_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

    repo = SwapRepository(db)
    order = repo.create(
        from_agent_id=agent_id,
        from_currency="BTC",
        from_amount=Decimal("0.01"),
        to_currency="XMR",
        to_amount=Decimal("1.5"),
        exchange_rate=Decimal("150.0"),
        fee_amount=Decimal("0.015"),
        htlc_hash=htlc_hash,
        lock_expiry=lock_expiry,
    )
    order.htlc_secret = secret
    db.flush()

    if external_order_id:
        repo.set_external_order(
            swap_id=order.id,
            external_order_id=external_order_id,
            deposit_address="bc1qdeposit123",
            provider_name=provider_name,
        )
        db.flush()
        db.refresh(order)

    return order


# ---------------------------------------------------------------------------
# SwapRepository.complete_from_external
# ---------------------------------------------------------------------------


class TestCompleteFromExternal:
    def test_transitions_created_to_completed(self, db):
        """complete_from_external() moves CREATED → COMPLETED and updates to_amount."""
        agent = _make_agent(db)
        order = _make_swap_order(db, agent.id, "ext-001")
        db.commit()

        repo = SwapRepository(db)
        rows = repo.complete_from_external(
            swap_id=order.id,
            to_amount=Decimal("1.8"),
            xmr_tx_hash="xmrhash123",
        )
        db.commit()

        assert rows == 1
        db.refresh(order)
        assert order.state == SwapStatus.COMPLETED
        assert order.to_amount == Decimal("1.8")
        assert order.xmr_tx_hash == "xmrhash123"

    def test_returns_zero_if_not_created(self, db):
        """complete_from_external() returns 0 if order is not in CREATED state."""
        agent = _make_agent(db)
        order = _make_swap_order(db, agent.id, "ext-002")
        db.commit()

        repo = SwapRepository(db)
        # First lock it via HTLC path
        repo.lock(order.id, btc_tx_hash="btctx")
        db.commit()

        rows = repo.complete_from_external(
            swap_id=order.id,
            to_amount=Decimal("1.5"),
        )
        assert rows == 0

    def test_works_without_xmr_tx_hash(self, db):
        """complete_from_external() succeeds without providing xmr_tx_hash."""
        agent = _make_agent(db)
        order = _make_swap_order(db, agent.id, "ext-003")
        db.commit()

        repo = SwapRepository(db)
        rows = repo.complete_from_external(
            swap_id=order.id,
            to_amount=Decimal("1.5"),
        )
        db.commit()

        assert rows == 1
        db.refresh(order)
        assert order.state == SwapStatus.COMPLETED
        assert order.xmr_tx_hash is None

    def test_returns_zero_for_unknown_id(self, db):
        """complete_from_external() returns 0 for non-existent order ID."""
        repo = SwapRepository(db)
        rows = repo.complete_from_external(
            swap_id=uuid.uuid4(),
            to_amount=Decimal("1.0"),
        )
        assert rows == 0


# ---------------------------------------------------------------------------
# SwapService.poll_external_orders
# ---------------------------------------------------------------------------


class TestPollExternalOrders:
    def test_completes_finished_order_and_credits_balance(self, db):
        """Finished orders are completed and XMR balance is credited."""
        agent = _make_agent(db)
        order = _make_swap_order(db, agent.id, "ext-cn-001", "changenow")
        db.commit()

        status_result = {
            "external_order_id": "ext-cn-001",
            "status": STATUS_FINISHED,
            "to_amount": "2.0",
            "provider": "changenow",
        }
        mock_cn = MagicMock()
        mock_cn.get_order_status.return_value = status_result

        svc = SwapService()
        with patch(
            "sthrip.services.swap_service.ChangeNowProvider",
            return_value=mock_cn,
        ):
            summary = svc.poll_external_orders(db)
        db.commit()

        assert summary["completed"] == 1
        assert summary["expired"] == 0
        assert summary["errors"] == 0

        db.refresh(order)
        assert order.state == SwapStatus.COMPLETED
        assert order.to_amount == Decimal("2.0")

        # Verify XMR balance was credited
        balance_repo = BalanceRepository(db)
        balance = balance_repo.get_or_create(agent.id, "XMR")
        assert balance.available >= Decimal("2.0")

    def test_expires_failed_order(self, db):
        """Failed exchange orders are transitioned to EXPIRED state."""
        agent = _make_agent(db)
        order = _make_swap_order(db, agent.id, "ext-cn-002", "changenow")
        db.commit()

        status_result = {
            "external_order_id": "ext-cn-002",
            "status": STATUS_FAILED,
            "to_amount": None,
            "provider": "changenow",
        }
        mock_cn = MagicMock()
        mock_cn.get_order_status.return_value = status_result

        svc = SwapService()
        with patch(
            "sthrip.services.swap_service.ChangeNowProvider",
            return_value=mock_cn,
        ):
            summary = svc.poll_external_orders(db)
        db.commit()

        assert summary["expired"] == 1
        assert summary["completed"] == 0
        db.refresh(order)
        assert order.state == SwapStatus.EXPIRED

    def test_expires_provider_expired_order(self, db):
        """Exchange-expired orders are transitioned to EXPIRED state."""
        agent = _make_agent(db)
        order = _make_swap_order(db, agent.id, "ext-ss-001", "sideshift")
        db.commit()

        status_result = {
            "external_order_id": "ext-ss-001",
            "status": STATUS_EXPIRED,
            "to_amount": None,
            "provider": "sideshift",
        }
        mock_ss = MagicMock()
        mock_ss.get_order_status.return_value = status_result

        svc = SwapService()
        with patch(
            "sthrip.services.swap_service.SideShiftProvider",
            return_value=mock_ss,
        ):
            summary = svc.poll_external_orders(db)
        db.commit()

        assert summary["expired"] == 1
        db.refresh(order)
        assert order.state == SwapStatus.EXPIRED

    def test_skips_waiting_order(self, db):
        """Orders still waiting/confirming are skipped without state change."""
        agent = _make_agent(db)
        order = _make_swap_order(db, agent.id, "ext-cn-003", "changenow")
        db.commit()

        status_result = {
            "external_order_id": "ext-cn-003",
            "status": STATUS_WAITING,
            "to_amount": None,
            "provider": "changenow",
        }
        mock_cn = MagicMock()
        mock_cn.get_order_status.return_value = status_result

        svc = SwapService()
        with patch(
            "sthrip.services.swap_service.ChangeNowProvider",
            return_value=mock_cn,
        ):
            summary = svc.poll_external_orders(db)

        assert summary["skipped"] == 1
        assert summary["completed"] == 0
        db.refresh(order)
        assert order.state == SwapStatus.CREATED

    def test_counts_provider_errors(self, db):
        """Provider errors are counted and logged; order is not changed."""
        agent = _make_agent(db)
        order = _make_swap_order(db, agent.id, "ext-cn-004", "changenow")
        db.commit()

        mock_cn = MagicMock()
        mock_cn.get_order_status.side_effect = ExchangeProviderError("network error")

        svc = SwapService()
        with patch(
            "sthrip.services.swap_service.ChangeNowProvider",
            return_value=mock_cn,
        ):
            summary = svc.poll_external_orders(db)

        assert summary["errors"] == 1
        assert summary["completed"] == 0
        db.refresh(order)
        assert order.state == SwapStatus.CREATED

    def test_handles_multiple_orders(self, db):
        """Polls all pending orders independently."""
        agent = _make_agent(db)
        order1 = _make_swap_order(db, agent.id, "ext-cn-m1", "changenow")
        order2 = _make_swap_order(db, agent.id, "ext-cn-m2", "changenow")
        order3 = _make_swap_order(db, agent.id, "ext-cn-m3", "changenow")
        db.commit()

        def _status(order_id):
            return {
                "ext-cn-m1": {"external_order_id": "ext-cn-m1", "status": STATUS_FINISHED, "to_amount": "1.5", "provider": "changenow"},
                "ext-cn-m2": {"external_order_id": "ext-cn-m2", "status": STATUS_FAILED, "to_amount": None, "provider": "changenow"},
                "ext-cn-m3": {"external_order_id": "ext-cn-m3", "status": STATUS_WAITING, "to_amount": None, "provider": "changenow"},
            }[order_id]

        mock_cn = MagicMock()
        mock_cn.get_order_status.side_effect = _status

        svc = SwapService()
        with patch(
            "sthrip.services.swap_service.ChangeNowProvider",
            return_value=mock_cn,
        ):
            summary = svc.poll_external_orders(db)
        db.commit()

        assert summary["completed"] == 1
        assert summary["expired"] == 1
        assert summary["skipped"] == 1
        assert summary["errors"] == 0

    def test_skips_orders_without_external_order_id(self, db):
        """Orders without external_order_id are not returned by get_pending_external
        (they belong to the legacy HTLC path), so poll summary has no changes."""
        agent = _make_agent(db)
        # Create without calling set_external_order
        _make_swap_order(db, agent.id, external_order_id=None)
        db.commit()

        svc = SwapService()
        summary = svc.poll_external_orders(db)

        # No orders in the pending_external list → all counts are 0
        assert summary["completed"] == 0
        assert summary["expired"] == 0
        assert summary["errors"] == 0
        # skipped is 0 because the order is not in the pending list
        assert summary["skipped"] == 0

    def test_uses_sideshift_provider_for_sideshift_orders(self, db):
        """Orders with provider_name='sideshift' use SideShiftProvider."""
        agent = _make_agent(db)
        order = _make_swap_order(db, agent.id, "ss-ext-001", "sideshift")
        db.commit()

        status_result = {
            "external_order_id": "ss-ext-001",
            "status": STATUS_FINISHED,
            "to_amount": "3.0",
            "provider": "sideshift",
        }
        mock_ss = MagicMock()
        mock_ss.get_order_status.return_value = status_result
        mock_cn = MagicMock()  # Should not be called

        svc = SwapService()
        with patch(
            "sthrip.services.swap_service.SideShiftProvider",
            return_value=mock_ss,
        ), patch(
            "sthrip.services.swap_service.ChangeNowProvider",
            return_value=mock_cn,
        ):
            summary = svc.poll_external_orders(db)
        db.commit()

        assert summary["completed"] == 1
        mock_cn.get_order_status.assert_not_called()

    def test_falls_back_to_to_amount_from_order_if_provider_returns_none(self, db):
        """When provider returns to_amount=None for FINISHED, uses order's to_amount."""
        agent = _make_agent(db)
        order = _make_swap_order(db, agent.id, "ext-cn-noamt", "changenow")
        db.commit()

        status_result = {
            "external_order_id": "ext-cn-noamt",
            "status": STATUS_FINISHED,
            "to_amount": None,
            "provider": "changenow",
        }
        mock_cn = MagicMock()
        mock_cn.get_order_status.return_value = status_result

        svc = SwapService()
        with patch(
            "sthrip.services.swap_service.ChangeNowProvider",
            return_value=mock_cn,
        ):
            summary = svc.poll_external_orders(db)
        db.commit()

        assert summary["completed"] == 1
        db.refresh(order)
        assert order.state == SwapStatus.COMPLETED
        # to_amount should be the original order.to_amount (1.5)
        assert order.to_amount == Decimal("1.5")

    def test_empty_pending_list_returns_zeros(self, db):
        """No pending orders → all counts are 0."""
        svc = SwapService()
        summary = svc.poll_external_orders(db)
        assert summary == {"completed": 0, "expired": 0, "errors": 0, "skipped": 0}

    def test_confirming_status_is_skipped(self, db):
        """Orders in confirming/exchanging/sending state are skipped."""
        agent = _make_agent(db)
        order = _make_swap_order(db, agent.id, "ext-cn-conf", "changenow")
        db.commit()

        status_result = {
            "external_order_id": "ext-cn-conf",
            "status": STATUS_CONFIRMING,
            "to_amount": None,
            "provider": "changenow",
        }
        mock_cn = MagicMock()
        mock_cn.get_order_status.return_value = status_result

        svc = SwapService()
        with patch(
            "sthrip.services.swap_service.ChangeNowProvider",
            return_value=mock_cn,
        ):
            summary = svc.poll_external_orders(db)

        assert summary["skipped"] == 1
        db.refresh(order)
        assert order.state == SwapStatus.CREATED


# ---------------------------------------------------------------------------
# Integration: expire_stale still works alongside the poller
# ---------------------------------------------------------------------------


class TestExpireStaleIntegration:
    def test_expire_stale_handles_orders_with_external_id(self, db):
        """expire_stale() works correctly for orders that also have an external_order_id."""
        import secrets as _secrets
        import hashlib

        agent = _make_agent(db)
        secret = _secrets.token_hex(32)
        htlc_hash = hashlib.sha256(bytes.fromhex(secret)).hexdigest()
        past_expiry = datetime.now(timezone.utc) - timedelta(minutes=5)

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
            lock_expiry=past_expiry,
        )
        repo.set_external_order(
            swap_id=order.id,
            external_order_id="stale-order",
            deposit_address="addr",
            provider_name="changenow",
        )
        db.commit()

        svc = SwapService()
        expired_count = svc.expire_stale(db)
        db.commit()

        assert expired_count == 1
        db.refresh(order)
        assert order.state == SwapStatus.EXPIRED
