"""Phase 2 Sprint 2 — integration tests for commission deduction on transfer.

Covers contract criteria 7-12: full atomic write of transaction + commission
deduction + fee_collections row + concurrency + idempotency + insufficient-
balance protection.
"""
from __future__ import annotations

import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.enums import AgentTier, FeeCollectionStatus
from sthrip.db.models import (
    Agent, AgentBalance, AgentReputation, Base, FeeCollection, Transaction,
)
from sthrip.db.transaction_repo import (
    InsufficientBalanceError,
    TransactionRepository,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def db_session():
    """In-memory SQLite + StaticPool for single-thread isolation."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[
        Agent.__table__,
        AgentReputation.__table__,
        AgentBalance.__table__,
        Transaction.__table__,
        FeeCollection.__table__,
    ])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_agent(db, name: str, tier: AgentTier, balance_piconero: int) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=name,
        tier=tier,
        is_active=True,
        xmr_address=f"4{name}xmr_addr_padding_to_long",
    )
    db.add(agent)
    db.flush()
    bal = AgentBalance(
        agent_id=agent.id,
        token="XMR",
        available=Decimal(balance_piconero),
        pending=Decimal(0),
    )
    db.add(bal)
    db.flush()
    return agent


def _balance_for(db, agent_id) -> int:
    bal = db.query(AgentBalance).filter(AgentBalance.agent_id == agent_id).first()
    return int(bal.available) if bal else 0


# ─────────────────────────────────────────────────────────────────────────────
# Contract criteria 7-10
# ─────────────────────────────────────────────────────────────────────────────


class TestCommissionDeduction:
    def test_commission_deducted_from_sender_at_create(self, db_session):
        """Free tier: 0.3% deducted from sender, full amount to receiver."""
        sender = _make_agent(db_session, "alice", AgentTier.FREE, 1_000_000)
        receiver = _make_agent(db_session, "bob", AgentTier.FREE, 0)

        repo = TransactionRepository(db_session)
        tx = repo.create_with_commission(
            tx_hash=f"hp_{uuid.uuid4().hex[:32]}",
            network="hub",
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount_piconero=100_000,
        )
        db_session.flush()

        # Free tier 0.3% of 100_000 = 300 piconero fee.
        assert _balance_for(db_session, sender.id) == 1_000_000 - 100_000 - 300
        assert _balance_for(db_session, sender.id) == 899_700
        assert _balance_for(db_session, receiver.id) == 100_000
        assert int(tx.fee) == 300

    def test_commission_deducted_pro_tier(self, db_session):
        """Verified (Pro) tier: 0.1% deducted."""
        sender = _make_agent(db_session, "alice", AgentTier.VERIFIED, 1_000_000)
        receiver = _make_agent(db_session, "bob", AgentTier.FREE, 0)

        repo = TransactionRepository(db_session)
        repo.create_with_commission(
            tx_hash=f"hp_{uuid.uuid4().hex[:32]}",
            network="hub",
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount_piconero=100_000,
        )
        db_session.flush()

        # Pro tier 0.1% of 100_000 = 100 piconero fee.
        assert _balance_for(db_session, sender.id) == 1_000_000 - 100_000 - 100
        assert _balance_for(db_session, sender.id) == 899_900
        assert _balance_for(db_session, receiver.id) == 100_000

    def test_fee_collection_row_inserted(self, db_session):
        """fee_collections row is recorded with all Sprint-2 columns."""
        sender = _make_agent(db_session, "alice", AgentTier.FREE, 1_000_000)
        receiver = _make_agent(db_session, "bob", AgentTier.FREE, 0)
        tx_hash = f"hp_{uuid.uuid4().hex[:32]}"

        repo = TransactionRepository(db_session)
        repo.create_with_commission(
            tx_hash=tx_hash,
            network="hub",
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount_piconero=100_000,
        )
        db_session.flush()

        rows = db_session.query(FeeCollection).filter(
            FeeCollection.payer_agent_id == sender.id
        ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.payer_agent_id == sender.id
        assert row.amount_piconero == 300
        assert row.rate_applied_bps == 30  # FREE tier
        assert row.transaction_ref == tx_hash
        assert row.collected_at is not None

    def test_fee_collection_row_pro_tier(self, db_session):
        """Pro-tier rate_applied_bps is recorded as 10."""
        sender = _make_agent(db_session, "alice", AgentTier.PREMIUM, 1_000_000)
        receiver = _make_agent(db_session, "bob", AgentTier.FREE, 0)

        repo = TransactionRepository(db_session)
        repo.create_with_commission(
            tx_hash=f"hp_{uuid.uuid4().hex[:32]}",
            network="hub",
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount_piconero=100_000,
        )
        db_session.flush()
        row = db_session.query(FeeCollection).filter(
            FeeCollection.payer_agent_id == sender.id
        ).first()
        assert row.rate_applied_bps == 10
        assert row.amount_piconero == 100

    def test_insufficient_balance_for_fee_blocks_transfer(self, db_session):
        """Sender has exactly amount but not amount + fee → reject, no mutation."""
        # 100_000 exactly (not enough to cover 300 fee on top).
        sender = _make_agent(db_session, "alice", AgentTier.FREE, 100_000)
        receiver = _make_agent(db_session, "bob", AgentTier.FREE, 0)

        repo = TransactionRepository(db_session)
        with pytest.raises(InsufficientBalanceError):
            repo.create_with_commission(
                tx_hash=f"hp_{uuid.uuid4().hex[:32]}",
                network="hub",
                from_agent_id=sender.id,
                to_agent_id=receiver.id,
                amount_piconero=100_000,
            )
        db_session.flush()

        # Balances unchanged.
        assert _balance_for(db_session, sender.id) == 100_000
        assert _balance_for(db_session, receiver.id) == 0
        # No fee_collections row inserted.
        assert (
            db_session.query(FeeCollection)
            .filter(FeeCollection.payer_agent_id == sender.id)
            .count()
            == 0
        )

    def test_insufficient_balance_subclasses_value_error(self, db_session):
        """Existing call-sites do ``except ValueError`` — subclassing is required."""
        assert issubclass(InsufficientBalanceError, ValueError)

    def test_dust_transfer_charges_floor(self, db_session):
        """Even 100-piconero transfer pays 1 piconero fee floor."""
        sender = _make_agent(db_session, "alice", AgentTier.FREE, 1_000)
        receiver = _make_agent(db_session, "bob", AgentTier.FREE, 0)

        repo = TransactionRepository(db_session)
        repo.create_with_commission(
            tx_hash=f"hp_{uuid.uuid4().hex[:32]}",
            network="hub",
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount_piconero=100,
        )
        db_session.flush()

        # 1_000 - 100 - 1 = 899
        assert _balance_for(db_session, sender.id) == 899
        assert _balance_for(db_session, receiver.id) == 100


# ─────────────────────────────────────────────────────────────────────────────
# Contract criterion 11 — concurrent transfers
# ─────────────────────────────────────────────────────────────────────────────


class TestConcurrencyAndIdempotency:
    def test_concurrent_transfers_no_double_fee(self, db_session):
        """Two sequential transfers (simulating serialized writes from a lock)
        produce exactly two fee_collections rows. SQLite-in-memory cannot
        truly run two threads against the same connection; we verify the
        non-double-charge invariant by issuing two transfers and asserting
        accounting integrity.

        On Postgres production, ``_get_for_update`` applies SELECT FOR UPDATE
        which serializes the two writers; the same invariant holds (each
        writer sees the post-update balance of the other before computing
        its own deduction).
        """
        sender = _make_agent(db_session, "alice", AgentTier.FREE, 1_000_000)
        receiver = _make_agent(db_session, "bob", AgentTier.FREE, 0)
        repo = TransactionRepository(db_session)

        repo.create_with_commission(
            tx_hash=f"hp_{uuid.uuid4().hex[:32]}",
            network="hub",
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount_piconero=100_000,
        )
        repo.create_with_commission(
            tx_hash=f"hp_{uuid.uuid4().hex[:32]}",
            network="hub",
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount_piconero=100_000,
        )
        db_session.flush()

        # Two transfers of 100_000 with 300 fee each → 600 fee total.
        assert _balance_for(db_session, sender.id) == 1_000_000 - 200_000 - 600
        assert _balance_for(db_session, receiver.id) == 200_000

        rows = db_session.query(FeeCollection).filter(
            FeeCollection.payer_agent_id == sender.id
        ).all()
        assert len(rows) == 2
        # No row was inserted twice for the same transaction_ref.
        refs = {r.transaction_ref for r in rows}
        assert len(refs) == 2

    def test_concurrent_transfers_one_rejected_no_double_fee(self, db_session):
        """If sender only has funds for one transfer, second must be rejected."""
        sender = _make_agent(db_session, "alice", AgentTier.FREE, 100_500)
        receiver = _make_agent(db_session, "bob", AgentTier.FREE, 0)
        repo = TransactionRepository(db_session)

        repo.create_with_commission(
            tx_hash=f"hp_{uuid.uuid4().hex[:32]}",
            network="hub",
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount_piconero=100_000,
        )
        db_session.flush()
        with pytest.raises(InsufficientBalanceError):
            repo.create_with_commission(
                tx_hash=f"hp_{uuid.uuid4().hex[:32]}",
                network="hub",
                from_agent_id=sender.id,
                to_agent_id=receiver.id,
                amount_piconero=100_000,
            )

        # Exactly one fee_collections row.
        rows = db_session.query(FeeCollection).filter(
            FeeCollection.payer_agent_id == sender.id
        ).all()
        assert len(rows) == 1

    def test_idempotency_replay_returns_cached_no_re_deduct(self, db_session):
        """Replay with same idempotency_key (and same tx_hash) returns the
        existing transaction without re-deducting fee or inserting a 2nd
        fee_collections row."""
        sender = _make_agent(db_session, "alice", AgentTier.FREE, 1_000_000)
        receiver = _make_agent(db_session, "bob", AgentTier.FREE, 0)
        tx_hash = f"hp_{uuid.uuid4().hex[:32]}"
        idem = "client-supplied-idem-1"

        repo = TransactionRepository(db_session)
        first = repo.create_with_commission(
            tx_hash=tx_hash,
            network="hub",
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount_piconero=100_000,
            idempotency_key=idem,
        )
        db_session.flush()
        balance_after_first = _balance_for(db_session, sender.id)

        # Replay: same tx_hash + idempotency_key → returns existing row, no mutation.
        second = repo.create_with_commission(
            tx_hash=tx_hash,
            network="hub",
            from_agent_id=sender.id,
            to_agent_id=receiver.id,
            amount_piconero=100_000,
            idempotency_key=idem,
        )
        db_session.flush()

        assert second.id == first.id
        assert _balance_for(db_session, sender.id) == balance_after_first
        # Still exactly one fee_collections row.
        rows = db_session.query(FeeCollection).filter(
            FeeCollection.payer_agent_id == sender.id
        ).all()
        assert len(rows) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Contract criterion 12 — legacy fee_collector unchanged
# ─────────────────────────────────────────────────────────────────────────────


class TestLegacyFeeCollectorCompatibility:
    def test_legacy_fee_collection_columns_still_writable(self, db_session):
        """Legacy fee_collector inserts FeeCollection rows with NULL in the
        new Sprint-2 columns. Verify both shapes coexist in one table."""
        from datetime import datetime, timezone
        legacy_row = FeeCollection(
            source_type="hub_routing",
            source_id=uuid.uuid4(),
            amount=Decimal("0.001"),
            token="XMR",
            status=FeeCollectionStatus.PENDING,
            # New Sprint-2 columns left NULL.
        )
        db_session.add(legacy_row)
        db_session.flush()

        sprint2_row = FeeCollection(
            source_type="fee_collection",
            source_id=uuid.uuid4(),
            amount=Decimal("0.0000000003"),
            token="XMR",
            status=FeeCollectionStatus.PENDING,
            payer_agent_id=uuid.uuid4(),
            amount_piconero=300,
            rate_applied_bps=30,
            transaction_ref="hp_abc",
            collected_at=datetime.now(timezone.utc),
        )
        db_session.add(sprint2_row)
        db_session.flush()

        # Both rows readable.
        assert db_session.query(FeeCollection).count() == 2
        assert (
            db_session.query(FeeCollection)
            .filter(FeeCollection.amount_piconero.is_(None))
            .count()
            == 1
        )
        assert (
            db_session.query(FeeCollection)
            .filter(FeeCollection.amount_piconero == 300)
            .count()
            == 1
        )
