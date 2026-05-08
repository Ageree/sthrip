"""Sprint 4b unit tests for the gated FK-drop migration.

These tests import the migration module directly and call ``upgrade()``
inside an alembic-style ``op`` context. We do not boot the full alembic
runner — we just exercise the gating logic and the column-drop plan.
"""
from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

# Direct import of the migration file by path — alembic's revision IDs
# don't map to Python module names cleanly.
_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "v3w4x5y6z7a8_drop_legacy_payment_fks.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "v3w4x5y6z7a8_drop_legacy_payment_fks", _MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _alembic_context(engine):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            yield conn


def _build_legacy_schema(engine: sa.Engine) -> None:
    """Create the four payment-graph tables with the legacy plaintext columns
    so the migration has something to drop."""
    metadata = sa.MetaData()
    sa.Table(
        "transactions",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("from_agent_id", sa.String(36), nullable=True),
        sa.Column("to_agent_id", sa.String(36), nullable=True),
        sa.Column("amount", sa.Numeric(20, 12), nullable=True),
        sa.Column("participant_envelope", sa.LargeBinary, nullable=True),
    )
    sa.Table(
        "escrow_deals",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("buyer_id", sa.String(36), nullable=True),
        sa.Column("seller_id", sa.String(36), nullable=True),
        sa.Column("amount", sa.Numeric(20, 12), nullable=True),
        sa.Column("participant_envelope", sa.LargeBinary, nullable=True),
    )
    sa.Table(
        "escrow_milestones",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("amount", sa.Numeric(20, 12), nullable=True),
        sa.Column("participant_envelope", sa.LargeBinary, nullable=True),
    )
    sa.Table(
        "message_relays",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("from_agent_id", sa.String(36), nullable=True),
        sa.Column("to_agent_id", sa.String(36), nullable=True),
        sa.Column("participant_envelope", sa.LargeBinary, nullable=True),
    )
    metadata.create_all(engine)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_fk_drop_migration_requires_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("STHRIP_DROP_LEGACY_FK", raising=False)
    module = _load_migration_module()
    engine = sa.create_engine(f"sqlite:///{tmp_path}/db.sqlite")
    _build_legacy_schema(engine)

    with _alembic_context(engine):
        with pytest.raises(RuntimeError) as exc:
            module.upgrade()
    assert "STHRIP_DROP_LEGACY_FK" in str(exc.value)


def test_fk_drop_migration_message_mentions_prereqs(monkeypatch, tmp_path):
    monkeypatch.delenv("STHRIP_DROP_LEGACY_FK", raising=False)
    module = _load_migration_module()
    engine = sa.create_engine(f"sqlite:///{tmp_path}/db.sqlite")
    _build_legacy_schema(engine)

    with _alembic_context(engine):
        with pytest.raises(RuntimeError) as exc:
            module.upgrade()

    msg = str(exc.value)
    # Operator runbook hints — must surface what to do BEFORE re-running.
    assert "sthrip-op-keystore" in msg
    assert "backfill_payment_envelope" in msg
    assert "STHRIP_READ_FROM_ENVELOPE" in msg


@pytest.mark.parametrize("falsy", ["", "false", "no", "0", "off"])
def test_fk_drop_migration_treats_falsy_as_unset(monkeypatch, tmp_path, falsy):
    monkeypatch.setenv("STHRIP_DROP_LEGACY_FK", falsy)
    module = _load_migration_module()
    engine = sa.create_engine(f"sqlite:///{tmp_path}/db.sqlite")
    _build_legacy_schema(engine)

    with _alembic_context(engine):
        with pytest.raises(RuntimeError):
            module.upgrade()


# ---------------------------------------------------------------------------
# Drop happy path
# ---------------------------------------------------------------------------


def test_fk_drop_migration_drops_columns(monkeypatch, tmp_path):
    monkeypatch.setenv("STHRIP_DROP_LEGACY_FK", "true")
    module = _load_migration_module()
    engine = sa.create_engine(f"sqlite:///{tmp_path}/db.sqlite")
    _build_legacy_schema(engine)

    with _alembic_context(engine):
        module.upgrade()

    insp = sa.inspect(engine)
    tx_cols = {c["name"] for c in insp.get_columns("transactions")}
    assert "from_agent_id" not in tx_cols
    assert "to_agent_id" not in tx_cols
    assert "amount" not in tx_cols
    assert "participant_envelope" in tx_cols

    es_cols = {c["name"] for c in insp.get_columns("escrow_deals")}
    assert "buyer_id" not in es_cols
    assert "seller_id" not in es_cols
    assert "amount" not in es_cols

    ms_cols = {c["name"] for c in insp.get_columns("escrow_milestones")}
    assert "amount" not in ms_cols

    mr_cols = {c["name"] for c in insp.get_columns("message_relays")}
    assert "from_agent_id" not in mr_cols
    assert "to_agent_id" not in mr_cols


def test_fk_drop_migration_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("STHRIP_DROP_LEGACY_FK", "true")
    module = _load_migration_module()
    engine = sa.create_engine(f"sqlite:///{tmp_path}/db.sqlite")
    _build_legacy_schema(engine)

    with _alembic_context(engine):
        module.upgrade()
    # Second run: every column already gone — should not raise.
    with _alembic_context(engine):
        module.upgrade()

    insp = sa.inspect(engine)
    tx_cols = {c["name"] for c in insp.get_columns("transactions")}
    assert "amount" not in tx_cols


def test_fk_drop_migration_skips_missing_table(monkeypatch, tmp_path):
    """If a table doesn't exist in this DB at all, the migration logs and
    moves on — no exception."""
    monkeypatch.setenv("STHRIP_DROP_LEGACY_FK", "true")
    module = _load_migration_module()
    engine = sa.create_engine(f"sqlite:///{tmp_path}/db.sqlite")
    # Build only one of the four tables.
    metadata = sa.MetaData()
    sa.Table(
        "transactions",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("from_agent_id", sa.String(36), nullable=True),
        sa.Column("to_agent_id", sa.String(36), nullable=True),
        sa.Column("amount", sa.Numeric(20, 12), nullable=True),
    )
    metadata.create_all(engine)

    with _alembic_context(engine):
        module.upgrade()

    insp = sa.inspect(engine)
    tx_cols = {c["name"] for c in insp.get_columns("transactions")}
    assert "from_agent_id" not in tx_cols


# ---------------------------------------------------------------------------
# Downgrade re-adds nullable columns
# ---------------------------------------------------------------------------


def test_fk_drop_migration_downgrade_readds_nullable(monkeypatch, tmp_path):
    monkeypatch.setenv("STHRIP_DROP_LEGACY_FK", "true")
    module = _load_migration_module()
    engine = sa.create_engine(f"sqlite:///{tmp_path}/db.sqlite")
    _build_legacy_schema(engine)

    with _alembic_context(engine):
        module.upgrade()
    with _alembic_context(engine):
        module.downgrade()

    insp = sa.inspect(engine)
    tx_cols = {c["name"] for c in insp.get_columns("transactions")}
    assert "from_agent_id" in tx_cols
    assert "amount" in tx_cols
