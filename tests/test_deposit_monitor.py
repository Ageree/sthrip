"""Tests for DepositMonitor — TDD RED phase"""
import asyncio
import pytest
from decimal import Decimal
from uuid import uuid4
from unittest.mock import MagicMock, AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, Transaction, SystemState,
    AgentTier, RateLimitTier, PrivacyLevel, TransactionStatus,
)
from sthrip.db.repository import BalanceRepository, TransactionRepository, SystemStateRepository


_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    Transaction.__table__,
    SystemState.__table__,
]


@pytest.fixture
def db_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    return engine


@pytest.fixture
def db_session_factory(db_engine):
    from contextlib import contextmanager
    _maker = sessionmaker(bind=db_engine)

    @contextmanager
    def _factory():
        session = _maker()
        try:
            yield session
        finally:
            session.close()

    return _factory


@pytest.fixture
def db_session(db_session_factory):
    with db_session_factory() as session:
        yield session


@pytest.fixture
def agent(db_session):
    agent = Agent(
        agent_name="monitor-test-agent",
        api_key_hash="monitorhash",
        tier=AgentTier.FREE,
        rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.MEDIUM,
        is_active=True,
    )
    db_session.add(agent)
    db_session.flush()

    # Set deposit address so monitor can map subaddress -> agent
    repo = BalanceRepository(db_session)
    repo.set_deposit_address(agent.id, "5AgentSubaddress001")
    db_session.flush()
    return agent


@pytest.fixture
def mock_wallet_service():
    svc = MagicMock()
    svc.get_incoming_transfers.return_value = []
    return svc


@pytest.fixture
def mock_webhook_fn():
    return MagicMock()


@pytest.fixture
def deposit_monitor(mock_wallet_service, db_session_factory, mock_webhook_fn):
    from sthrip.services.deposit_monitor import DepositMonitor
    return DepositMonitor(
        wallet_service=mock_wallet_service,
        db_session_factory=db_session_factory,
        min_confirmations=10,
        webhook_fn=mock_webhook_fn,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# DEPOSIT DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

class TestDepositDetection:
    """Test that monitor detects incoming deposits."""

    def test_poll_with_no_transfers(self, deposit_monitor, db_session):
        deposit_monitor.poll_once()
        # No crash, no side effects

    def test_detects_new_deposit(self, deposit_monitor, mock_wallet_service, agent, db_session):
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_deposit_001",
                "amount": Decimal("2.5"),
                "confirmations": 3,
                "height": 100_000,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5AgentSubaddress001",
            },
        ]
        deposit_monitor.poll_once()

        # Transaction should be created in DB
        tx_repo = TransactionRepository(db_session)
        tx = tx_repo.get_by_hash("tx_deposit_001")
        assert tx is not None
        assert tx.amount == Decimal("2.5")
        assert tx.status == TransactionStatus.PENDING

    def test_pending_balance_updated_on_new_deposit(
        self, deposit_monitor, mock_wallet_service, agent, db_session
    ):
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_deposit_pending",
                "amount": Decimal("1.0"),
                "confirmations": 3,
                "height": 100_000,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5AgentSubaddress001",
            },
        ]
        deposit_monitor.poll_once()

        repo = BalanceRepository(db_session)
        balance = repo.get_or_create(agent.id)
        assert balance.pending == Decimal("1.0")
        assert (balance.available or Decimal("0")) == Decimal("0")


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIRMATION TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfirmationTracking:
    """Test confirmation updates and balance crediting."""

    def test_updates_confirmations(
        self, deposit_monitor, mock_wallet_service, agent, db_session
    ):
        # First poll: 3 confirmations
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_confirm_track",
                "amount": Decimal("5.0"),
                "confirmations": 3,
                "height": 100_000,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5AgentSubaddress001",
            },
        ]
        deposit_monitor.poll_once()

        # Second poll: 8 confirmations
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_confirm_track",
                "amount": Decimal("5.0"),
                "confirmations": 8,
                "height": 100_000,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5AgentSubaddress001",
            },
        ]
        deposit_monitor.poll_once()

        tx_repo = TransactionRepository(db_session)
        tx = tx_repo.get_by_hash("tx_confirm_track")
        assert tx.confirmations == 8
        assert tx.status == TransactionStatus.PENDING

    def test_credits_balance_after_min_confirmations(
        self, deposit_monitor, mock_wallet_service, agent, db_session
    ):
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_fully_confirmed",
                "amount": Decimal("3.0"),
                "confirmations": 15,
                "height": 100_000,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5AgentSubaddress001",
            },
        ]
        deposit_monitor.poll_once()

        repo = BalanceRepository(db_session)
        balance = repo.get_or_create(agent.id)
        assert balance.available == Decimal("3.0")
        assert balance.total_deposited == Decimal("3.0")
        assert (balance.pending or Decimal("0")) == Decimal("0")

    def test_confirmed_tx_status(
        self, deposit_monitor, mock_wallet_service, agent, db_session
    ):
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_status_check",
                "amount": Decimal("1.0"),
                "confirmations": 10,
                "height": 100_000,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5AgentSubaddress001",
            },
        ]
        deposit_monitor.poll_once()

        tx_repo = TransactionRepository(db_session)
        tx = tx_repo.get_by_hash("tx_status_check")
        assert tx.status == TransactionStatus.CONFIRMED

    def test_webhook_sent_on_confirmation(
        self, deposit_monitor, mock_wallet_service, mock_webhook_fn, agent, db_session
    ):
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_webhook_test",
                "amount": Decimal("2.0"),
                "confirmations": 12,
                "height": 100_000,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5AgentSubaddress001",
            },
        ]
        deposit_monitor.poll_once()

        mock_webhook_fn.assert_called_once()
        call_args = mock_webhook_fn.call_args
        assert call_args[0][0] == str(agent.id)
        assert call_args[0][1] == "payment.deposit_confirmed"


# ═══════════════════════════════════════════════════════════════════════════════
# IDEMPOTENCY
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdempotency:
    """Test that duplicate polls don't double-credit."""

    def test_no_duplicate_transaction_creation(
        self, deposit_monitor, mock_wallet_service, agent, db_session
    ):
        transfer = {
            "txid": "tx_idempotent",
            "amount": Decimal("4.0"),
            "confirmations": 5,
            "height": 100_000,
            "subaddr_index": {"major": 0, "minor": 1},
            "address": "5AgentSubaddress001",
        }
        mock_wallet_service.get_incoming_transfers.return_value = [transfer]

        deposit_monitor.poll_once()
        deposit_monitor.poll_once()

        tx_repo = TransactionRepository(db_session)
        txs = db_session.query(Transaction).filter(
            Transaction.tx_hash == "tx_idempotent"
        ).all()
        assert len(txs) == 1

    def test_no_double_credit_after_confirmation(
        self, deposit_monitor, mock_wallet_service, agent, db_session
    ):
        transfer = {
            "txid": "tx_no_double",
            "amount": Decimal("5.0"),
            "confirmations": 15,
            "height": 100_000,
            "subaddr_index": {"major": 0, "minor": 1},
            "address": "5AgentSubaddress001",
        }
        mock_wallet_service.get_incoming_transfers.return_value = [transfer]

        deposit_monitor.poll_once()
        deposit_monitor.poll_once()

        repo = BalanceRepository(db_session)
        balance = repo.get_or_create(agent.id)
        # Should be credited exactly once
        assert balance.available == Decimal("5.0")
        assert balance.total_deposited == Decimal("5.0")

    def test_pending_to_confirmed_transition(
        self, deposit_monitor, mock_wallet_service, agent, db_session
    ):
        """Deposit starts pending, then reaches confirmations -> credit."""
        # First poll: not enough confirmations
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_transition",
                "amount": Decimal("7.0"),
                "confirmations": 5,
                "height": 100_000,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5AgentSubaddress001",
            },
        ]
        deposit_monitor.poll_once()

        # Refresh session to see monitor's commits
        db_session.expire_all()
        repo = BalanceRepository(db_session)
        balance = repo.get_or_create(agent.id)
        assert balance.pending == Decimal("7.0")
        assert (balance.available or Decimal("0")) == Decimal("0")

        # Second poll: enough confirmations
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_transition",
                "amount": Decimal("7.0"),
                "confirmations": 12,
                "height": 100_000,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5AgentSubaddress001",
            },
        ]
        deposit_monitor.poll_once()

        db_session.expire_all()
        balance = repo.get_or_create(agent.id)
        assert balance.available == Decimal("7.0")
        assert balance.total_deposited == Decimal("7.0")
        assert (balance.pending or Decimal("0")) == Decimal("0")


# ═══════════════════════════════════════════════════════════════════════════════
# SUBADDRESS MAPPING
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubaddressMapping:
    """Test mapping subaddress -> agent_id."""

    def test_unknown_subaddress_ignored(
        self, deposit_monitor, mock_wallet_service, db_session
    ):
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_unknown_addr",
                "amount": Decimal("1.0"),
                "confirmations": 15,
                "height": 100_000,
                "subaddr_index": {"major": 0, "minor": 99},
                "address": "5UnknownSubaddress",
            },
        ]
        deposit_monitor.poll_once()

        tx_repo = TransactionRepository(db_session)
        tx = tx_repo.get_by_hash("tx_unknown_addr")
        assert tx is None  # Should not create transaction for unknown address


# ═══════════════════════════════════════════════════════════════════════════════
# LAST SCANNED HEIGHT
# ═══════════════════════════════════════════════════════════════════════════════

class TestLastScannedHeight:
    """Test height tracking for incremental scanning."""

    def test_passes_last_height_to_wallet_service(
        self, deposit_monitor, mock_wallet_service, agent, db_session
    ):
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_height_track",
                "amount": Decimal("1.0"),
                "confirmations": 15,
                "height": 50_000,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5AgentSubaddress001",
            },
        ]
        deposit_monitor.poll_once()

        # Second poll should use last known height
        mock_wallet_service.get_incoming_transfers.return_value = []
        deposit_monitor.poll_once()

        last_call = mock_wallet_service.get_incoming_transfers.call_args
        assert last_call[1].get("min_height", 0) >= 50_000


# ═══════════════════════════════════════════════════════════════════════════════
# HEIGHT PERSISTENCE VIA SYSTEM STATE
# ═══════════════════════════════════════════════════════════════════════════════

class TestHeightPersistence:
    """Test that last_scanned_height is persisted via SystemStateRepository."""

    def test_saves_height_after_poll(
        self, deposit_monitor, mock_wallet_service, agent, db_session
    ):
        """After processing transfers, height should be saved to system_state."""
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_persist_height",
                "amount": Decimal("1.0"),
                "confirmations": 15,
                "height": 42_000,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5AgentSubaddress001",
            },
        ]
        deposit_monitor.poll_once()

        # Check system_state table
        state_repo = SystemStateRepository(db_session)
        db_session.expire_all()
        saved = state_repo.get("last_scanned_height")
        assert saved is not None
        assert int(saved) == 42_000

    def test_loads_height_on_init(self, mock_wallet_service, db_session_factory):
        """DepositMonitor should load persisted height from SystemState."""
        # Pre-set height in DB
        with db_session_factory() as db:
            state_repo = SystemStateRepository(db)
            state_repo.set("last_scanned_height", "99000")
            db.commit()

        from sthrip.services.deposit_monitor import DepositMonitor
        monitor = DepositMonitor(
            wallet_service=mock_wallet_service,
            db_session_factory=db_session_factory,
            min_confirmations=10,
        )
        monitor.load_persisted_height()

        assert monitor._last_height == 99_000

    def test_height_survives_restart(
        self, mock_wallet_service, db_session_factory, db_session, agent
    ):
        """Simulate restart: poll, stop, create new monitor, verify height."""
        from sthrip.services.deposit_monitor import DepositMonitor

        # First monitor polls and saves height
        m1 = DepositMonitor(
            wallet_service=mock_wallet_service,
            db_session_factory=db_session_factory,
            min_confirmations=10,
        )
        mock_wallet_service.get_incoming_transfers.return_value = [
            {
                "txid": "tx_survive",
                "amount": Decimal("2.0"),
                "confirmations": 15,
                "height": 77_777,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5AgentSubaddress001",
            },
        ]
        m1.poll_once()

        # Second monitor loads persisted height
        m2 = DepositMonitor(
            wallet_service=mock_wallet_service,
            db_session_factory=db_session_factory,
            min_confirmations=10,
        )
        m2.load_persisted_height()
        assert m2._last_height == 77_777
