"""Sprint 3 integration tests: dual-write payment-graph envelope.

Each repo-level write site (Transaction, EscrowDeal, EscrowMilestone,
MessageRelay) must populate ``participant_envelope`` alongside the existing
plaintext FKs. Reads are unchanged in this sprint — Sprint 4 cuts over.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from sthrip.db import models
from sthrip.db.escrow_repo import EscrowRepository
from sthrip.db.milestone_repo import MilestoneRepository
from sthrip.db.transaction_repo import TransactionRepository
from sthrip.services.envelope_crypto import (
    PaymentEnvelope,
    decrypt_envelope,
    load_hub_kek,
)
from sthrip.services.operator_keystore import get_keystore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(db, name: str) -> models.Agent:
    agent = models.Agent(
        agent_name=name,
        api_key_hash=uuid.uuid4().hex,
    )
    db.add(agent)
    db.flush()
    return agent


def _decrypt(blob: bytes) -> dict:
    env = PaymentEnvelope.from_bytes(blob)
    return decrypt_envelope(
        env,
        load_hub_kek(),
        get_keystore().get_kek_for_envelope(),
    )


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


def test_transaction_envelope_written(db_session_factory):
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")

        repo = TransactionRepository(db)
        tx = repo.create(
            tx_hash=uuid.uuid4().hex,
            network="monero",
            from_agent_id=a.id,
            to_agent_id=b.id,
            amount=Decimal("12.5"),
            memo="paying for service",
        )
        db.commit()
        db.refresh(tx)

        assert tx.participant_envelope is not None
        assert isinstance(tx.participant_envelope, (bytes, bytearray))
        assert len(tx.participant_envelope) >= 80
        assert tx.amount_bucket == "10-100 XMR"

        out = _decrypt(tx.participant_envelope)
        assert out["from_agent_id"] == str(a.id)
        assert out["to_agent_id"] == str(b.id)
        assert out["amount"] == "12.5"
        assert out["description"] == "paying for service"
    finally:
        db.close()


def test_transaction_envelope_uses_fresh_dek(db_session_factory):
    """Two transactions produce different envelopes even with identical inputs."""
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        repo = TransactionRepository(db)

        tx1 = repo.create(
            tx_hash=uuid.uuid4().hex, network="monero",
            from_agent_id=a.id, to_agent_id=b.id, amount=Decimal("1.0"),
        )
        tx2 = repo.create(
            tx_hash=uuid.uuid4().hex, network="monero",
            from_agent_id=a.id, to_agent_id=b.id, amount=Decimal("1.0"),
        )
        db.commit()
        assert tx1.participant_envelope != tx2.participant_envelope
    finally:
        db.close()


def test_transaction_envelope_idempotent_on_replay(db_session_factory):
    """If model already has an envelope, the writer does not overwrite it."""
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        repo = TransactionRepository(db)

        tx = repo.create(
            tx_hash=uuid.uuid4().hex, network="monero",
            from_agent_id=a.id, to_agent_id=b.id, amount=Decimal("1.0"),
        )
        db.commit()
        first = bytes(tx.participant_envelope)

        # Manually invoke the writer again — should be a no-op.
        from sthrip.services.payment_envelope_writer import apply_envelope
        apply_envelope(
            tx,
            from_agent_id=a.id, to_agent_id=b.id,
            amount=Decimal("1.0"), description=None,
        )
        assert bytes(tx.participant_envelope) == first
    finally:
        db.close()


def test_reads_unchanged_in_dual_write(db_session_factory):
    """Existing list_by_agent / get_by_hash still work via plaintext FKs."""
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        repo = TransactionRepository(db)

        tx_hash = uuid.uuid4().hex
        tx = repo.create(
            tx_hash=tx_hash, network="monero",
            from_agent_id=a.id, to_agent_id=b.id, amount=Decimal("3.0"),
        )
        db.commit()

        # Reads still go through plaintext FKs in Sprint 3.
        fetched = repo.get_by_hash(tx_hash)
        assert fetched is not None
        assert fetched.from_agent_id == a.id
        assert fetched.to_agent_id == b.id
        assert fetched.amount == Decimal("3.0")

        rows = repo.list_by_agent(a.id, direction="out")
        assert len(rows) == 1
        assert rows[0].tx_hash == tx_hash
    finally:
        db.close()


# ---------------------------------------------------------------------------
# EscrowDeal
# ---------------------------------------------------------------------------


def test_escrow_envelope_written(db_session_factory):
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        repo = EscrowRepository(db)

        deal = repo.create(
            deal_hash=uuid.uuid4().hex[:32],
            buyer_id=a.id,
            seller_id=b.id,
            amount=Decimal("250"),
            description="research delivery",
        )
        db.commit()

        assert deal.participant_envelope is not None
        assert deal.amount_bucket == "100-1k XMR"

        out = _decrypt(deal.participant_envelope)
        assert out["from_agent_id"] == str(a.id)  # buyer maps to "from"
        assert out["to_agent_id"] == str(b.id)
        assert out["amount"] == "250"
        assert out["description"] == "research delivery"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# EscrowMilestone
# ---------------------------------------------------------------------------


def test_milestone_envelope_written(db_session_factory):
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        deal = EscrowRepository(db).create(
            deal_hash=uuid.uuid4().hex[:32],
            buyer_id=a.id,
            seller_id=b.id,
            amount=Decimal("30"),
            description="parent",
        )
        db.commit()

        m_repo = MilestoneRepository(db)
        milestones = m_repo.create_milestones(
            deal.id,
            [
                {"description": "phase 1", "amount": Decimal("10"),
                 "delivery_timeout_hours": 24, "review_timeout_hours": 24},
                {"description": "phase 2", "amount": Decimal("20"),
                 "delivery_timeout_hours": 48, "review_timeout_hours": 24},
            ],
            fee_percent=Decimal("0.001"),
        )
        db.commit()

        assert len(milestones) == 2
        for ms in milestones:
            assert ms.participant_envelope is not None
            assert ms.amount_bucket in ("10-100 XMR",)

        out0 = _decrypt(milestones[0].participant_envelope)
        assert out0["from_agent_id"] == str(a.id)
        assert out0["to_agent_id"] == str(b.id)
        assert out0["amount"] == "10"
        assert out0["description"] == "phase 1"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# MessageRelay
# ---------------------------------------------------------------------------


def test_message_relay_envelope_written(db_session_factory):
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")

        # MessageRelay has a 24h TTL; we instantiate directly through the
        # service path that the dual-write hook is wired into.
        from sthrip.services.payment_envelope_writer import apply_envelope
        from datetime import timedelta

        relay = models.MessageRelay(
            from_agent_id=a.id,
            to_agent_id=b.id,
            payment_id="pay-test-1",
            ciphertext="ct",
            nonce="nc",
            sender_public_key="pk",
            size_bytes=2,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        apply_envelope(
            relay,
            from_agent_id=a.id, to_agent_id=b.id,
            amount=None, description="pay-test-1",
        )
        db.add(relay)
        db.commit()

        assert relay.participant_envelope is not None
        out = _decrypt(relay.participant_envelope)
        assert out["from_agent_id"] == str(a.id)
        assert out["to_agent_id"] == str(b.id)
        assert out["description"] == "pay-test-1"
        assert out["amount"] is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Migration round-trip
# ---------------------------------------------------------------------------


def _baseline_engine():
    """Build an engine with the four target tables WITHOUT the new envelope
    columns, so the migration sees a 'pre-Sprint-3' schema."""
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()

    sa.Table(
        "transactions", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tx_hash", sa.String(255), nullable=False),
    )
    sa.Table(
        "escrow_deals", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    sa.Table(
        "escrow_milestones", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    sa.Table(
        "message_relays", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    metadata.create_all(engine)
    return engine


def test_migration_round_trip(monkeypatch):
    """Manually invoke the Sprint 3 migration's upgrade/downgrade.

    We avoid alembic's full env.py wiring (test fixtures use a different DB)
    and instead drive the migration body against an isolated engine via
    op.get_bind() monkey-patching.
    """
    from migrations.versions import s0t1u2v3w4x5_payment_envelope as mig
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = _baseline_engine()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        ops = Operations(ctx)
        # Replace the alembic op singleton's get_bind with our local context
        # by stubbing the module-level "op" used in the migration.
        import alembic
        orig_op = alembic.op
        try:
            alembic.op = ops
            mig.op = ops  # the migration imported `op` at top-level
            mig.upgrade()
        finally:
            alembic.op = orig_op
            mig.op = orig_op  # restore so other tests aren't affected

    # Verify columns now exist
    insp = sa_inspect(engine)
    for table in ("transactions", "escrow_deals", "escrow_milestones"):
        cols = {c["name"] for c in insp.get_columns(table)}
        assert "participant_envelope" in cols, f"{table} missing envelope"
        assert "amount_bucket" in cols, f"{table} missing bucket"
    msg_cols = {c["name"] for c in insp.get_columns("message_relays")}
    assert "participant_envelope" in msg_cols
    assert "amount_bucket" not in msg_cols  # message_relays has no bucket

    # Re-running upgrade is idempotent
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        ops = Operations(ctx)
        import alembic
        orig_op = alembic.op
        try:
            alembic.op = ops
            mig.op = ops
            mig.upgrade()  # no-op
        finally:
            alembic.op = orig_op
            mig.op = orig_op

    # Downgrade removes the columns.
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        ops = Operations(ctx)
        import alembic
        orig_op = alembic.op
        try:
            alembic.op = ops
            mig.op = ops
            mig.downgrade()
        finally:
            alembic.op = orig_op
            mig.op = orig_op

    insp = sa_inspect(engine)
    for table in ("transactions", "escrow_deals", "escrow_milestones", "message_relays"):
        cols = {c["name"] for c in insp.get_columns(table)}
        assert "participant_envelope" not in cols, f"{table} envelope not dropped"
        assert "amount_bucket" not in cols, f"{table} bucket not dropped"
