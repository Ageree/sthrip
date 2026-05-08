"""Sprint 4a: tests for the rerun-safe payment-envelope backfill script.

The backfill must:
- write envelopes for legacy rows (envelope IS NULL)
- skip rows that already have an envelope
- be safe to re-run (idempotent)
- cover all 4 payment-graph tables
- not mutate when --dry-run is set
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

# Ensure scripts/ is importable.
_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import backfill_payment_envelope as bf  # type: ignore[import-not-found]
from sthrip.db import models
from sthrip.db.models import EscrowStatus, MilestoneStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(db, name: str) -> models.Agent:
    a = models.Agent(agent_name=name, api_key_hash=uuid.uuid4().hex)
    db.add(a)
    db.flush()
    return a


def _make_legacy_transaction(db, a, b, amount=Decimal("5"), memo="legacy"):
    tx = models.Transaction(
        tx_hash=uuid.uuid4().hex,
        network="monero",
        from_agent_id=a.id,
        to_agent_id=b.id,
        amount=amount,
        memo=memo,
        participant_envelope=None,
    )
    db.add(tx)
    return tx


def _make_legacy_escrow(db, a, b, amount=Decimal("100")):
    deal = models.EscrowDeal(
        deal_hash=uuid.uuid4().hex[:32],
        buyer_id=a.id,
        seller_id=b.id,
        amount=amount,
        description="legacy-deal",
        status=EscrowStatus.CREATED,
        participant_envelope=None,
    )
    db.add(deal)
    db.flush()
    return deal


def _make_legacy_milestone(db, escrow_id, amount=Decimal("10"), seq=1):
    ms = models.EscrowMilestone(
        escrow_id=escrow_id,
        sequence=seq,
        description=f"phase-{seq}",
        amount=amount,
        delivery_timeout_hours=24,
        review_timeout_hours=24,
        status=MilestoneStatus.PENDING,
        participant_envelope=None,
    )
    db.add(ms)
    return ms


def _make_legacy_message(db, a, b):
    relay = models.MessageRelay(
        from_agent_id=a.id,
        to_agent_id=b.id,
        payment_id="pay-legacy",
        ciphertext="ct",
        nonce="nc",
        sender_public_key="pk",
        size_bytes=2,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        participant_envelope=None,
    )
    db.add(relay)
    return relay


# ---------------------------------------------------------------------------
# Per-table backfill
# ---------------------------------------------------------------------------


def test_backfill_writes_envelope_for_legacy_transactions(db_session_factory):
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        for _ in range(3):
            _make_legacy_transaction(db, a, b)
        db.commit()

        results = bf.run_backfill(db, table_filter="transactions", batch_size=10)
        assert results["transactions"]["processed"] == 3
        assert results["transactions"]["skipped"] == 0

        # All rows now have envelopes.
        rows = db.query(models.Transaction).all()
        assert len(rows) == 3
        for row in rows:
            assert row.participant_envelope is not None
            assert len(row.participant_envelope) >= 80
            assert row.amount_bucket is not None
    finally:
        db.close()


def test_backfill_skips_existing(db_session_factory):
    """Rows with non-null envelope must NOT be re-encrypted."""
    db = db_session_factory()
    try:
        from sthrip.db.transaction_repo import TransactionRepository
        repo = TransactionRepository(db)
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        existing = repo.create(
            tx_hash=uuid.uuid4().hex, network="monero",
            from_agent_id=a.id, to_agent_id=b.id, amount=Decimal("1"),
        )
        db.commit()
        original_blob = bytes(existing.participant_envelope)

        # Seed a legacy row alongside.
        _make_legacy_transaction(db, a, b)
        db.commit()

        results = bf.run_backfill(db, table_filter="transactions", batch_size=10)
        # Only the legacy row was processed.
        assert results["transactions"]["processed"] == 1

        # Existing envelope is unchanged.
        existing_after = db.query(models.Transaction).filter(
            models.Transaction.id == existing.id
        ).first()
        assert bytes(existing_after.participant_envelope) == original_blob
    finally:
        db.close()


def test_backfill_idempotent(db_session_factory):
    """Running backfill twice must not duplicate work or modify rows."""
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        for _ in range(2):
            _make_legacy_transaction(db, a, b)
        db.commit()

        first = bf.run_backfill(db, table_filter="transactions", batch_size=10)
        assert first["transactions"]["processed"] == 2

        # Snapshot envelopes after the first pass.
        snapshots = {
            row.id: bytes(row.participant_envelope)
            for row in db.query(models.Transaction).all()
        }

        # Second pass: zero new processes, no changes.
        second = bf.run_backfill(db, table_filter="transactions", batch_size=10)
        assert second["transactions"]["processed"] == 0

        for row in db.query(models.Transaction).all():
            assert bytes(row.participant_envelope) == snapshots[row.id]
    finally:
        db.close()


def test_backfill_dry_run_doesnt_write(db_session_factory):
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        _make_legacy_transaction(db, a, b)
        db.commit()

        # Dry run should report 1 row that WOULD be processed but write 0.
        results = bf.run_backfill(
            db, table_filter="transactions", batch_size=10, dry_run=True,
        )
        assert results["transactions"]["processed"] == 1

        # The row's envelope must still be NULL.
        row = db.query(models.Transaction).first()
        assert row.participant_envelope is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# All four tables together
# ---------------------------------------------------------------------------


def test_backfill_covers_all_tables(db_session_factory):
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")

        _make_legacy_transaction(db, a, b)
        deal = _make_legacy_escrow(db, a, b)
        db.flush()
        _make_legacy_milestone(db, deal.id, seq=1)
        _make_legacy_milestone(db, deal.id, seq=2)
        _make_legacy_message(db, a, b)
        db.commit()

        results = bf.run_backfill(db, batch_size=10)
        assert results["transactions"]["processed"] == 1
        assert results["escrow_deals"]["processed"] == 1
        assert results["escrow_milestones"]["processed"] == 2
        assert results["message_relays"]["processed"] == 1

        # Sanity-check all rows now have envelopes.
        for tx in db.query(models.Transaction).all():
            assert tx.participant_envelope is not None
        for d in db.query(models.EscrowDeal).all():
            assert d.participant_envelope is not None
        for ms in db.query(models.EscrowMilestone).all():
            assert ms.participant_envelope is not None
        for m in db.query(models.MessageRelay).all():
            assert m.participant_envelope is not None
    finally:
        db.close()


def test_backfill_milestone_uses_parent_buyer_seller(db_session_factory):
    """Milestone envelopes must encode the PARENT escrow's buyer/seller."""
    db = db_session_factory()
    try:
        from sthrip.services.envelope_crypto import (
            PaymentEnvelope, decrypt_envelope, load_hub_kek,
        )
        from sthrip.services.operator_keystore import get_keystore

        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        deal = _make_legacy_escrow(db, a, b)
        db.flush()
        ms = _make_legacy_milestone(db, deal.id, amount=Decimal("7"), seq=1)
        db.commit()

        bf.run_backfill(db, table_filter="escrow_milestones", batch_size=10)
        db.refresh(ms)
        assert ms.participant_envelope is not None

        env = PaymentEnvelope.from_bytes(bytes(ms.participant_envelope))
        payload = decrypt_envelope(
            env, load_hub_kek(), get_keystore().get_kek_for_envelope(),
        )
        assert payload["from_agent_id"] == str(a.id)  # buyer maps to "from"
        assert payload["to_agent_id"] == str(b.id)
        assert payload["amount"] == "7"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


def test_backfill_processes_multiple_batches(db_session_factory):
    """Verify the batching loop terminates correctly when rows exceed batch size."""
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        # 5 legacy rows, batch size 2 → 3 iterations expected.
        for _ in range(5):
            _make_legacy_transaction(db, a, b)
        db.commit()

        results = bf.run_backfill(db, table_filter="transactions", batch_size=2)
        assert results["transactions"]["processed"] == 5

        for row in db.query(models.Transaction).all():
            assert row.participant_envelope is not None
    finally:
        db.close()
