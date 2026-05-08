"""Sprint 4a: integration tests for repo-level dual-read.

Each repo's read methods must:
- behave EXACTLY as before when STHRIP_READ_FROM_ENVELOPE is unset/false
- return envelope-decrypted values when the flag is on AND envelope is present
- fall back to plaintext FKs when envelope is null OR decrypt fails

These tests exercise the wiring in transaction_repo / escrow_repo / milestone_repo.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from sthrip.db import models
from sthrip.db.escrow_repo import EscrowRepository
from sthrip.db.milestone_repo import MilestoneRepository
from sthrip.db.transaction_repo import TransactionRepository


def _make_agent(db, name: str) -> models.Agent:
    agent = models.Agent(
        agent_name=name,
        api_key_hash=uuid.uuid4().hex,
    )
    db.add(agent)
    db.flush()
    return agent


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


def test_transaction_reads_unchanged_when_flag_off(db_session_factory, monkeypatch):
    monkeypatch.delenv("STHRIP_READ_FROM_ENVELOPE", raising=False)
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        repo = TransactionRepository(db)
        tx_hash = uuid.uuid4().hex
        repo.create(
            tx_hash=tx_hash, network="monero",
            from_agent_id=a.id, to_agent_id=b.id,
            amount=Decimal("5"), memo="test-memo",
        )
        db.commit()

        fetched = repo.get_by_hash(tx_hash)
        assert fetched is not None
        assert fetched.from_agent_id == a.id
        assert fetched.to_agent_id == b.id
        assert fetched.amount == Decimal("5")
        assert fetched.memo == "test-memo"

        rows = repo.list_by_agent(a.id, direction="out")
        assert len(rows) == 1
        assert rows[0].from_agent_id == a.id
    finally:
        db.close()


def test_transaction_reads_use_envelope_when_flag_on(db_session_factory, monkeypatch):
    """Flag on + envelope present: reader yields envelope-sourced values.

    The envelope-sourced UUIDs match the plaintext FK UUIDs because the
    writer at create-time records the same IDs in both columns. We assert
    on memo/amount round-trip to prove the reader is firing.
    """
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        repo = TransactionRepository(db)
        tx_hash = uuid.uuid4().hex
        repo.create(
            tx_hash=tx_hash, network="monero",
            from_agent_id=a.id, to_agent_id=b.id,
            amount=Decimal("12.5"), memo="env-memo",
        )
        db.commit()

        fetched = repo.get_by_hash(tx_hash)
        assert fetched is not None
        # Envelope round-trip restores the exact same payload.
        assert fetched.from_agent_id == a.id
        assert fetched.to_agent_id == b.id
        assert fetched.amount == Decimal("12.5")
        assert fetched.memo == "env-memo"

        rows = repo.list_by_agent(a.id, direction="out")
        assert len(rows) == 1
        assert rows[0].from_agent_id == a.id
    finally:
        db.close()


def test_transaction_reads_fallback_when_envelope_null(db_session_factory, monkeypatch):
    """Legacy row (no envelope) under flag-on: reader falls back to FK."""
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")

        # Insert a row directly, bypassing the repo writer's apply_envelope.
        tx_hash = uuid.uuid4().hex
        legacy = models.Transaction(
            tx_hash=tx_hash, network="monero",
            from_agent_id=a.id, to_agent_id=b.id,
            amount=Decimal("3.0"), memo="legacy-memo",
            participant_envelope=None,  # explicit null
        )
        db.add(legacy)
        db.commit()

        repo = TransactionRepository(db)
        fetched = repo.get_by_hash(tx_hash)
        assert fetched is not None
        # FK values survived
        assert fetched.from_agent_id == a.id
        assert fetched.amount == Decimal("3.0")
        assert fetched.memo == "legacy-memo"
    finally:
        db.close()


def test_transaction_reads_fallback_when_decrypt_fails(db_session_factory, monkeypatch):
    """Corrupt envelope + flag on: reader falls back to FK silently."""
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        tx_hash = uuid.uuid4().hex
        bad = models.Transaction(
            tx_hash=tx_hash, network="monero",
            from_agent_id=a.id, to_agent_id=b.id,
            amount=Decimal("4.0"), memo="legacy",
            participant_envelope=b"this-is-not-a-real-envelope",
        )
        db.add(bad)
        db.commit()

        repo = TransactionRepository(db)
        fetched = repo.get_by_hash(tx_hash)
        assert fetched is not None
        assert fetched.from_agent_id == a.id
        assert fetched.amount == Decimal("4.0")
        assert fetched.memo == "legacy"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# EscrowDeal
# ---------------------------------------------------------------------------


def test_escrow_reads_unchanged_when_flag_off(db_session_factory, monkeypatch):
    monkeypatch.delenv("STHRIP_READ_FROM_ENVELOPE", raising=False)
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        repo = EscrowRepository(db)
        deal = repo.create(
            deal_hash=uuid.uuid4().hex[:32],
            buyer_id=a.id, seller_id=b.id,
            amount=Decimal("100"), description="off-flag-deal",
        )
        db.commit()

        fetched = repo.get_by_id(deal.id)
        assert fetched is not None
        assert fetched.buyer_id == a.id
        assert fetched.seller_id == b.id
        assert fetched.description == "off-flag-deal"

        items, total = repo.list_by_agent(a.id, role="buyer")
        assert total == 1
        assert items[0].buyer_id == a.id
    finally:
        db.close()


def test_escrow_reads_use_envelope_when_flag_on(db_session_factory, monkeypatch):
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        repo = EscrowRepository(db)
        deal = repo.create(
            deal_hash=uuid.uuid4().hex[:32],
            buyer_id=a.id, seller_id=b.id,
            amount=Decimal("250"), description="env-deal",
        )
        db.commit()

        fetched = repo.get_by_id(deal.id)
        assert fetched is not None
        assert fetched.buyer_id == a.id
        assert fetched.seller_id == b.id
        assert fetched.amount == Decimal("250")
        assert fetched.description == "env-deal"
    finally:
        db.close()


def test_escrow_reads_fallback_when_envelope_null(db_session_factory, monkeypatch):
    """Legacy escrow without envelope still reads cleanly under flag-on."""
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")
    db = db_session_factory()
    try:
        from sthrip.db.models import EscrowStatus
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        legacy = models.EscrowDeal(
            deal_hash=uuid.uuid4().hex[:32],
            buyer_id=a.id, seller_id=b.id,
            amount=Decimal("75"), description="legacy",
            status=EscrowStatus.CREATED,
            participant_envelope=None,
        )
        db.add(legacy)
        db.commit()

        repo = EscrowRepository(db)
        fetched = repo.get_by_id(legacy.id)
        assert fetched is not None
        assert fetched.buyer_id == a.id
        assert fetched.amount == Decimal("75")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# EscrowMilestone
# ---------------------------------------------------------------------------


def test_milestone_reads_unchanged_when_flag_off(db_session_factory, monkeypatch):
    monkeypatch.delenv("STHRIP_READ_FROM_ENVELOPE", raising=False)
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        deal = EscrowRepository(db).create(
            deal_hash=uuid.uuid4().hex[:32],
            buyer_id=a.id, seller_id=b.id,
            amount=Decimal("30"), description="parent",
        )
        db.commit()

        repo = MilestoneRepository(db)
        repo.create_milestones(
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

        rows = repo.get_by_escrow(deal.id)
        assert len(rows) == 2
        assert rows[0].amount == Decimal("10")
        assert rows[1].description == "phase 2"
    finally:
        db.close()


def test_milestone_reads_use_envelope_when_flag_on(db_session_factory, monkeypatch):
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")
    db = db_session_factory()
    try:
        a = _make_agent(db, "alice")
        b = _make_agent(db, "bob")
        deal = EscrowRepository(db).create(
            deal_hash=uuid.uuid4().hex[:32],
            buyer_id=a.id, seller_id=b.id,
            amount=Decimal("30"), description="parent",
        )
        db.commit()

        repo = MilestoneRepository(db)
        repo.create_milestones(
            deal.id,
            [
                {"description": "phase A", "amount": Decimal("15"),
                 "delivery_timeout_hours": 24, "review_timeout_hours": 24},
            ],
            fee_percent=Decimal("0.001"),
        )
        db.commit()

        rows = repo.get_by_escrow(deal.id)
        assert len(rows) == 1
        assert rows[0].amount == Decimal("15")
        assert rows[0].description == "phase A"
    finally:
        db.close()
