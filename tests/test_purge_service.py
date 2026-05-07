"""Phase 2 Sprint 1 — auto-purge service tests.

Drives ``sthrip.services.purge_service`` against an in-memory SQLite engine
(no Postgres required). Exercises the contract acceptance criteria
listed in ``.harness/phase2-money-and-tee/sprint-1-contract.md``:

1. test_purge_deletes_old_transactions
2. test_purge_respects_active_references
3. test_purge_skips_non_terminal_status
4. test_chain_rolling_reset_keeps_new_chain_valid
9. test_run_full_purge_writes_metadata_row
10. test_retention_days_env_validation
"""
from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Agent,
    AuditLog,
    Base,
    CanaryState,
    EscrowDeal,
    EscrowMilestone,
    IpSalt,
    MessageRelay,
    MultisigEscrow,
    PurgeMetadata,
    Transaction,
)
from sthrip.db.enums import (
    EscrowStatus,
    MilestoneStatus,
    TransactionStatus,
)
from sthrip.services import purge_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _naive_utc_now() -> datetime:
    """SQLite-compatible naive UTC timestamp (matches existing test patterns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def db_session():
    """Fresh in-memory SQLite session with all tables touched by the purge."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        Agent.__table__,
        Transaction.__table__,
        EscrowDeal.__table__,
        EscrowMilestone.__table__,
        MessageRelay.__table__,
        MultisigEscrow.__table__,
        IpSalt.__table__,
        AuditLog.__table__,
        PurgeMetadata.__table__,
        CanaryState.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_agent(db, *, name_prefix: str = "agent") -> Agent:
    """Insert a minimal Agent row sufficient for FK references."""
    a = Agent(
        id=uuid.uuid4(),
        agent_name=f"{name_prefix}-{secrets.token_hex(4)}",
        api_key_hash=secrets.token_hex(32),
        is_active=True,
    )
    db.add(a)
    db.flush()
    return a


def _make_transaction(
    db,
    *,
    age_days: float,
    status: TransactionStatus = TransactionStatus.CONFIRMED,
    from_agent: Agent | None = None,
    to_agent: Agent | None = None,
) -> Transaction:
    created = _naive_utc_now() - timedelta(days=age_days)
    tx = Transaction(
        id=uuid.uuid4(),
        tx_hash=secrets.token_hex(32),
        network="monero",
        token="XMR",
        from_agent_id=from_agent.id if from_agent is not None else None,
        to_agent_id=to_agent.id if to_agent is not None else None,
        amount=Decimal("1.0"),
        status=status,
        created_at=created,
    )
    db.add(tx)
    db.flush()
    return tx


def _make_escrow_deal(
    db,
    *,
    age_days: float,
    status: EscrowStatus,
    buyer: Agent,
    seller: Agent,
) -> EscrowDeal:
    created = _naive_utc_now() - timedelta(days=age_days)
    deal = EscrowDeal(
        id=uuid.uuid4(),
        deal_hash=secrets.token_hex(32),
        buyer_id=buyer.id,
        seller_id=seller.id,
        amount=Decimal("1.0"),
        token="XMR",
        status=status,
        created_at=created,
    )
    db.add(deal)
    db.flush()
    return deal


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_purge_deletes_old_transactions(db_session):
    """Contract #1: 50 old terminal txs deleted, 50 recent retained."""
    a = _make_agent(db_session, name_prefix="alice")
    b = _make_agent(db_session, name_prefix="bob")

    for _ in range(50):
        _make_transaction(
            db_session,
            age_days=70,
            status=TransactionStatus.CONFIRMED,
            from_agent=a,
            to_agent=b,
        )
    for _ in range(50):
        _make_transaction(
            db_session,
            age_days=10,
            status=TransactionStatus.CONFIRMED,
            from_agent=a,
            to_agent=b,
        )
    db_session.commit()

    deleted = purge_service.purge_transactions(60, db=db_session)
    db_session.commit()

    assert deleted == 50
    remaining = db_session.query(Transaction).count()
    assert remaining == 50


def test_purge_respects_active_references(db_session):
    """Contract #2: an old terminal tx referenced by an active escrow stays."""
    a = _make_agent(db_session, name_prefix="alice")
    b = _make_agent(db_session, name_prefix="bob")

    tx = _make_transaction(
        db_session,
        age_days=120,
        status=TransactionStatus.CONFIRMED,
        from_agent=a,
        to_agent=b,
    )
    # Active (non-terminal) escrow referencing the same agents.
    _make_escrow_deal(
        db_session,
        age_days=1,
        status=EscrowStatus.ACCEPTED,
        buyer=a,
        seller=b,
    )
    db_session.commit()

    deleted = purge_service.purge_transactions(60, db=db_session)
    db_session.commit()

    assert deleted == 0
    survivors = db_session.query(Transaction).filter(Transaction.id == tx.id).count()
    assert survivors == 1


def test_purge_skips_non_terminal_status(db_session):
    """Contract #3: an old PENDING tx is NOT deleted."""
    a = _make_agent(db_session, name_prefix="alice")
    b = _make_agent(db_session, name_prefix="bob")

    tx = _make_transaction(
        db_session,
        age_days=70,
        status=TransactionStatus.PENDING,
        from_agent=a,
        to_agent=b,
    )
    db_session.commit()

    deleted = purge_service.purge_transactions(60, db=db_session)
    db_session.commit()

    assert deleted == 0
    survivors = db_session.query(Transaction).filter(Transaction.id == tx.id).count()
    assert survivors == 1


def test_purge_message_relays_no_status_guard(db_session):
    """Bonus: relays don't have terminal state — old rows always purgeable."""
    a = _make_agent(db_session, name_prefix="alice")
    b = _make_agent(db_session, name_prefix="bob")

    old = MessageRelay(
        id=uuid.uuid4(),
        from_agent_id=a.id,
        to_agent_id=b.id,
        ciphertext="x" * 16,
        nonce=secrets.token_hex(12),
        sender_public_key=secrets.token_hex(32),
        size_bytes=16,
        expires_at=_naive_utc_now() + timedelta(hours=1),
        created_at=_naive_utc_now() - timedelta(days=70),
    )
    fresh = MessageRelay(
        id=uuid.uuid4(),
        from_agent_id=a.id,
        to_agent_id=b.id,
        ciphertext="y" * 16,
        nonce=secrets.token_hex(12),
        sender_public_key=secrets.token_hex(32),
        size_bytes=16,
        expires_at=_naive_utc_now() + timedelta(hours=1),
        created_at=_naive_utc_now() - timedelta(days=10),
    )
    db_session.add_all([old, fresh])
    db_session.commit()

    deleted = purge_service.purge_message_relays(60, db=db_session)
    db_session.commit()

    assert deleted == 1
    remaining_ids = {r.id for r in db_session.query(MessageRelay).all()}
    assert fresh.id in remaining_ids
    assert old.id not in remaining_ids


def test_chain_rolling_reset_keeps_new_chain_valid(db_session, monkeypatch):
    """Contract #4: post-purge, new audit events form a verifiable chain."""
    # Configure dev env so the audit_logger writes via our db session and
    # _GENESIS_HMAC short-circuits ip_salt unavailability gracefully.
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("AUDIT_HMAC_KEY", "dev-audit-hmac-key-change-in-prod")
    from sthrip.config import get_settings
    get_settings.cache_clear()

    # Seed 10 OLD audit_log rows directly. We bypass log_event so the
    # rows are clearly "purgeable" — their HMACs are filled but unrelated
    # to any current key, and the test only cares about deletion + reset.
    base = _naive_utc_now() - timedelta(days=120)
    prev = "0" * 64
    for i in range(10):
        row = AuditLog(
            id=uuid.uuid4(),
            agent_id=None,
            action=f"legacy.event.{i}",
            success=True,
            created_at=base + timedelta(seconds=i),
            prev_hmac=prev,
            entry_hmac=secrets.token_hex(32),
        )
        prev = row.entry_hmac
        db_session.add(row)
    db_session.commit()

    result = purge_service.purge_audit_log(60, db=db_session)
    db_session.commit()

    assert result["deleted"] == 10
    assert result["reset_row_id"] is not None

    # Verify the reset row exists with NULL chain pointers (legacy marker).
    reset = (
        db_session.query(AuditLog)
        .filter(AuditLog.id == uuid.UUID(result["reset_row_id"]))
        .one()
    )
    assert reset.action == "chain_reset"
    assert reset.prev_hmac is None
    assert reset.entry_hmac is None

    # Write 3 new events via log_event. They must form a valid chain.
    from sthrip.services.audit_logger import log_event, verify_chain
    for i in range(3):
        log_event(
            action="test.action",
            details={"action": f"new-event-{i}"},
            db=db_session,
        )
    db_session.commit()

    status = verify_chain(db_session)
    assert status.ok is True, f"chain broken: first_bad_id={status.first_bad_id}"
    # The 3 new events were checked (legacy reset row is skipped).
    assert status.total_checked == 3


def test_run_full_purge_writes_metadata_row(db_session):
    """Contract #9: orchestrator writes one purge_metadata row."""
    a = _make_agent(db_session, name_prefix="alice")
    b = _make_agent(db_session, name_prefix="bob")

    for _ in range(3):
        _make_transaction(
            db_session,
            age_days=80,
            status=TransactionStatus.CONFIRMED,
            from_agent=a,
            to_agent=b,
        )
    for _ in range(2):
        _make_escrow_deal(
            db_session,
            age_days=80,
            status=EscrowStatus.COMPLETED,
            buyer=a,
            seller=b,
        )
    db_session.commit()

    summary = purge_service.run_full_purge(60, db=db_session)
    db_session.commit()

    rows = db_session.query(PurgeMetadata).all()
    assert len(rows) == 1
    md = rows[0]
    assert md.transactions_deleted == 3
    assert md.escrow_deals_deleted == 2
    total = (
        md.transactions_deleted
        + md.escrow_deals_deleted
        + md.escrow_milestones_deleted
        + md.message_relays_deleted
        + md.audit_log_deleted
    )
    assert total > 0
    # Summary mirrors the row.
    assert summary["transactions_deleted"] == 3
    assert summary["escrow_deals_deleted"] == 2


def test_retention_days_env_validation(monkeypatch):
    """Contract #10: STHRIP_DATA_RETENTION_DAYS validated at Settings load."""
    from sthrip.config import Settings, get_settings
    get_settings.cache_clear()

    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key-for-tests-long-enough-32")

    monkeypatch.setenv("STHRIP_DATA_RETENTION_DAYS", "5")
    with pytest.raises(Exception) as exc:
        Settings()
    assert "STHRIP_DATA_RETENTION_DAYS" in str(exc.value) or "between 7 and 365" in str(exc.value)

    monkeypatch.setenv("STHRIP_DATA_RETENTION_DAYS", "400")
    with pytest.raises(Exception) as exc:
        Settings()
    assert "between 7 and 365" in str(exc.value) or "STHRIP_DATA_RETENTION_DAYS" in str(exc.value)

    monkeypatch.setenv("STHRIP_DATA_RETENTION_DAYS", "60")
    settings = Settings()
    assert settings.sthrip_data_retention_days == 60

    get_settings.cache_clear()


def test_purge_orchestrator_handles_empty_database(db_session):
    """Bonus: orchestrator on empty DB writes a metadata row with zero counts."""
    summary = purge_service.run_full_purge(60, db=db_session)
    db_session.commit()

    md = db_session.query(PurgeMetadata).one()
    assert md.transactions_deleted == 0
    assert md.escrow_deals_deleted == 0
    assert md.escrow_milestones_deleted == 0
    assert md.message_relays_deleted == 0
    assert md.audit_log_deleted == 0
    assert md.audit_chain_reset_at_id is None
    assert summary["audit_log_deleted"] == 0
