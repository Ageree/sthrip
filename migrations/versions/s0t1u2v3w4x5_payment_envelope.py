"""Sprint 3 (anonymity-hardening): payment-graph envelope columns (dual-write).

Revision ID: s0t1u2v3w4x5
Revises: r9s0t1u2v3w4
Create Date: 2026-05-06

Migration steps
---------------
Adds the following nullable columns (forward-only, no backfill of existing
rows; per spec line 346 backfill is Sprint 4's job):

* ``transactions.participant_envelope`` — BYTEA / LargeBinary
* ``transactions.amount_bucket`` — VARCHAR(32)
* ``escrow_deals.participant_envelope`` — BYTEA / LargeBinary
* ``escrow_deals.amount_bucket`` — VARCHAR(32)
* ``escrow_milestones.participant_envelope`` — BYTEA / LargeBinary
* ``escrow_milestones.amount_bucket`` — VARCHAR(32)
* ``message_relays.participant_envelope`` — BYTEA / LargeBinary
  (no amount_bucket — relays don't carry an amount)

Idempotency
-----------
Each column add inspects the existing schema first and skips if already
present. Re-running the migration is a no-op. Compatible with PostgreSQL
and SQLite (the test suite uses an in-memory SQLite engine).

Downgrade
---------
Drops the columns added by this migration. Destructive of any envelope data
that has already been written; Sprint 3 is dual-write so plaintext FKs are
still authoritative and a downgrade does not lose the graph itself.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger("alembic.env")

# Revision identifiers
revision = "s0t1u2v3w4x5"
down_revision = "r9s0t1u2v3w4"
branch_labels = None
depends_on = None


# Tables and the columns we are adding.
_ENVELOPE_TABLES = (
    ("transactions", True),         # has amount_bucket
    ("escrow_deals", True),
    ("escrow_milestones", True),
    ("message_relays", False),      # no amount, no bucket
)


def _add_column_if_missing(insp, table: str, column: sa.Column) -> None:
    existing = {col["name"] for col in insp.get_columns(table)}
    if column.name in existing:
        return
    op.add_column(table, column)
    logger.info("Added %s.%s", table, column.name)


def _drop_column_if_present(insp, table: str, name: str) -> None:
    existing = {col["name"] for col in insp.get_columns(table)}
    if name not in existing:
        return
    op.drop_column(table, name)
    logger.info("Dropped %s.%s", table, name)


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    for table, has_bucket in _ENVELOPE_TABLES:
        _add_column_if_missing(
            insp,
            table,
            sa.Column("participant_envelope", sa.LargeBinary(), nullable=True),
        )
        if has_bucket:
            _add_column_if_missing(
                insp,
                table,
                sa.Column("amount_bucket", sa.String(length=32), nullable=True),
            )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    for table, has_bucket in _ENVELOPE_TABLES:
        if has_bucket:
            _drop_column_if_present(insp, table, "amount_bucket")
        _drop_column_if_present(insp, table, "participant_envelope")
