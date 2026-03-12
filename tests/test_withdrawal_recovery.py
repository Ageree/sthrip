"""Tests for pending withdrawal recovery on startup."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import Base, Agent, AgentReputation, AgentBalance, PendingWithdrawal


_TEST_TABLES = [
    Agent.__table__, AgentReputation.__table__,
    AgentBalance.__table__, PendingWithdrawal.__table__,
]


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_get_stale_pending_returns_old_records(db_session):
    """get_stale_pending returns withdrawals older than threshold."""
    from sthrip.db.repository import PendingWithdrawalRepository

    agent_id = uuid.uuid4()
    pw = PendingWithdrawal(
        agent_id=agent_id,
        amount=Decimal("1.0"),
        address="addr_stale",
        status="pending",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    db_session.add(pw)
    db_session.flush()

    repo = PendingWithdrawalRepository(db_session)
    stale = repo.get_stale_pending(max_age_minutes=5)
    assert len(stale) == 1
    assert stale[0].id == pw.id


def test_get_stale_pending_ignores_recent(db_session):
    """get_stale_pending ignores records younger than threshold."""
    from sthrip.db.repository import PendingWithdrawalRepository

    agent_id = uuid.uuid4()
    pw = PendingWithdrawal(
        agent_id=agent_id,
        amount=Decimal("1.0"),
        address="addr_recent",
        status="pending",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    db_session.add(pw)
    db_session.flush()

    repo = PendingWithdrawalRepository(db_session)
    stale = repo.get_stale_pending(max_age_minutes=5)
    assert len(stale) == 0


def test_recovery_marks_completed_when_tx_found():
    """Recovery marks pending as completed when wallet shows matching tx."""
    from sthrip.services.withdrawal_recovery import recover_pending_withdrawals

    mock_pw = MagicMock()
    mock_pw.id = "pw-1"
    mock_pw.address = "addr_found"
    mock_pw.amount = Decimal("1.5")
    mock_pw.agent_id = "agent-1"

    mock_wallet = MagicMock()
    mock_wallet.get_outgoing_transfers.return_value = [
        {"address": "addr_found", "amount": 1.5, "tx_hash": "abc123"}
    ]

    mock_pw_repo = MagicMock()
    mock_pw_repo.get_stale_pending.return_value = [mock_pw]

    recovered = recover_pending_withdrawals(
        pw_repo=mock_pw_repo,
        wallet_service=mock_wallet,
    )
    mock_pw_repo.mark_completed.assert_called_once_with("pw-1", tx_hash="abc123")
    assert recovered == 1


def test_recovery_marks_needs_review_when_no_tx():
    """Recovery marks pending as needs_review (NOT failed+credit) when no matching tx."""
    from sthrip.services.withdrawal_recovery import recover_pending_withdrawals

    mock_pw = MagicMock()
    mock_pw.id = "pw-2"
    mock_pw.address = "addr_missing"
    mock_pw.amount = Decimal("2.0")
    mock_pw.agent_id = "agent-2"
    mock_pw.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    mock_wallet = MagicMock()
    mock_wallet.get_outgoing_transfers.return_value = []

    mock_pw_repo = MagicMock()
    mock_pw_repo.get_stale_pending.return_value = [mock_pw]

    mock_bal_repo = MagicMock()

    recovered = recover_pending_withdrawals(
        pw_repo=mock_pw_repo,
        wallet_service=mock_wallet,
        balance_repo=mock_bal_repo,
    )
    # Must NOT auto-credit
    mock_bal_repo.credit.assert_not_called()
    mock_pw_repo.mark_failed.assert_not_called()
    # Must mark needs_review
    mock_pw_repo.mark_needs_review.assert_called_once_with(
        "pw-2",
        reason="No matching on-chain tx after max_age_minutes",
    )
    assert recovered == 1


def test_recovery_empty_outgoing_does_not_auto_credit():
    """Empty outgoing list must NOT trigger auto-credit — marks needs_review."""
    from sthrip.services.withdrawal_recovery import recover_pending_withdrawals

    mock_pw = MagicMock()
    mock_pw.id = "pw-3"
    mock_pw.address = "addr_nocredit"
    mock_pw.amount = Decimal("5.0")
    mock_pw.agent_id = "agent-3"
    mock_pw.created_at = datetime.now(timezone.utc) - timedelta(minutes=15)

    mock_wallet = MagicMock()
    mock_wallet.get_outgoing_transfers.return_value = []

    mock_pw_repo = MagicMock()
    mock_pw_repo.get_stale_pending.return_value = [mock_pw]

    mock_bal_repo = MagicMock()

    recover_pending_withdrawals(
        pw_repo=mock_pw_repo,
        wallet_service=mock_wallet,
        balance_repo=mock_bal_repo,
    )

    mock_bal_repo.credit.assert_not_called()
    mock_pw_repo.mark_needs_review.assert_called_once()


def test_recovery_logs_critical_for_unmatched_stale():
    """Logger emits CRITICAL for unmatched stale withdrawals."""
    import logging
    from sthrip.services.withdrawal_recovery import recover_pending_withdrawals

    mock_pw = MagicMock()
    mock_pw.id = "pw-4"
    mock_pw.address = "addr_critical"
    mock_pw.amount = Decimal("3.0")
    mock_pw.agent_id = "agent-4"
    mock_pw.created_at = datetime.now(timezone.utc) - timedelta(minutes=20)

    mock_wallet = MagicMock()
    mock_wallet.get_outgoing_transfers.return_value = []

    mock_pw_repo = MagicMock()
    mock_pw_repo.get_stale_pending.return_value = [mock_pw]

    with pytest.raises(AssertionError) if False else \
            _capture_log("sthrip", logging.CRITICAL) as captured:
        recover_pending_withdrawals(
            pw_repo=mock_pw_repo,
            wallet_service=mock_wallet,
        )

    assert len(captured) >= 1
    assert "HUMAN_ACTION_REQUIRED" in captured[0]
    assert "pw-4" in captured[0]


def test_find_matching_transfer_rejects_timestamp_delta_over_threshold():
    """_find_matching_transfer rejects a match when timestamp delta > 30 min."""
    from sthrip.services.withdrawal_recovery import _find_matching_transfer

    mock_pw = MagicMock()
    mock_pw.address = "addr_ts"
    mock_pw.amount = Decimal("1.0")
    mock_pw.created_at = datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc)

    # TX timestamp 2 hours after pw.created_at — should be rejected
    outgoing = [{
        "address": "addr_ts",
        "amount": 1.0,
        "tx_hash": "hash_ts",
        "timestamp": datetime(2026, 3, 9, 14, 0, 0, tzinfo=timezone.utc),
    }]

    result = _find_matching_transfer(mock_pw, outgoing)
    assert result is None


def test_find_matching_transfer_accepts_timestamp_within_threshold():
    """_find_matching_transfer accepts a match when timestamp delta <= 30 min."""
    from sthrip.services.withdrawal_recovery import _find_matching_transfer

    mock_pw = MagicMock()
    mock_pw.address = "addr_ts_ok"
    mock_pw.amount = Decimal("1.0")
    mock_pw.created_at = datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc)

    outgoing = [{
        "address": "addr_ts_ok",
        "amount": 1.0,
        "tx_hash": "hash_ts_ok",
        "timestamp": datetime(2026, 3, 9, 12, 15, 0, tzinfo=timezone.utc),
    }]

    result = _find_matching_transfer(mock_pw, outgoing)
    assert result is not None
    assert result["tx_hash"] == "hash_ts_ok"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import contextlib
import logging


@contextlib.contextmanager
def _capture_log(logger_name: str, level: int):
    """Capture log messages at the given level from a named logger."""
    captured: list[str] = []
    logger = logging.getLogger(logger_name)

    class _Handler(logging.Handler):
        def emit(self, record):
            if record.levelno >= level:
                captured.append(self.format(record))

    handler = _Handler()
    handler.setLevel(level)
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(level)
    try:
        yield captured
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
