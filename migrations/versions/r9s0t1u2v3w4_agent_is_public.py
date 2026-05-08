"""Sprint 2 (anonymity-hardening): Marketplace ``is_public`` opt-in.

Revision ID: r9s0t1u2v3w4
Revises: q8r9s0t1u2v3
Create Date: 2026-05-06

Migration steps
---------------
1. Add ``agents.is_public BOOLEAN NOT NULL DEFAULT false`` (idempotent: skipped
   if column already exists).
2. Create supporting index ``ix_agents_is_public`` on ``agents.is_public``
   (idempotent).
3. Existing rows automatically pick up the ``server_default='false'`` value —
   per Lead decision Q3 (hard cut), no grace period, no email-blast in the
   migration. Operators are notified via PRIVACY_FEATURES.md / SDK CHANGELOG.

Idempotency
-----------
Both the column add and the index creation inspect existing schema before
acting. Re-running the migration on an already-upgraded database is a no-op.
SQLite is fully supported (the test suite uses an in-memory SQLite engine);
the column is created with the same shape under both dialects.

Downgrade
---------
Drops the index and the column. Rolling back is destructive of the new flag
— no agent visibility data is preserved across a downgrade.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger("alembic.env")

# Revision identifiers
revision = "r9s0t1u2v3w4"
down_revision = "q8r9s0t1u2v3"
branch_labels = None
depends_on = None


_INDEX_NAME = "ix_agents_is_public"


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    # --- Step 1: add column if missing -----------------------------------
    agent_cols = {col["name"] for col in insp.get_columns("agents")}
    if "is_public" not in agent_cols:
        op.add_column(
            "agents",
            sa.Column(
                "is_public",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        logger.info("Added agents.is_public column with default=false")

    # --- Step 2: index for the marketplace filter -------------------------
    existing_indexes = {ix["name"] for ix in insp.get_indexes("agents")}
    if _INDEX_NAME not in existing_indexes:
        op.create_index(_INDEX_NAME, "agents", ["is_public"])
        logger.info("Created index %s on agents.is_public", _INDEX_NAME)


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    existing_indexes = {ix["name"] for ix in insp.get_indexes("agents")}
    if _INDEX_NAME in existing_indexes:
        op.drop_index(_INDEX_NAME, table_name="agents")

    agent_cols = {col["name"] for col in insp.get_columns("agents")}
    if "is_public" in agent_cols:
        op.drop_column("agents", "is_public")
