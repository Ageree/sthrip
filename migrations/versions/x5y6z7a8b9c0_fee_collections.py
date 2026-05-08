"""Phase 2 Sprint 2 (revenue-and-tee): fee_collections commission columns.

Revision ID: x5y6z7a8b9c0
Revises: w4x5y6z7a8b9
Create Date: 2026-05-07

Extends the pre-existing ``fee_collections`` table (created in earlier
revisions for the legacy 1% hub-routing fee) with the columns required by
the new Sprint 2 commission-on-transfer subsystem:

* ``payer_agent_id`` — FK to ``agents.id``; populated for commission rows.
* ``amount_piconero`` — integer piconero amount (avoids float drift).
* ``rate_applied_bps`` — basis points (30 for Free, 10 for Pro+).
* ``transaction_ref`` — string ref to the originating transaction
  (tx_hash for hub-routing rows).
* ``collected_at`` — TIMESTAMPTZ for revenue aggregation queries.

Two new indexes support per-agent and global MTD revenue queries:

* ``ix_fee_collections_payer_collected`` — ``(payer_agent_id, collected_at)``
* ``ix_fee_collections_collected_at`` — ``(collected_at)``

Idempotency: every ADD / DROP inspects the schema first, mirroring Sprint 1
discipline. Re-runs after success are a no-op. Downgrade drops the new
columns and indexes only — pre-existing fee rows survive intact.

Migration is backward-compatible: legacy rows have NULL in the new columns
which is fine because the legacy fee_collector code never reads them.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger("alembic.env")

# Revision identifiers
revision = "x5y6z7a8b9c0"
down_revision = "w4x5y6z7a8b9"
branch_labels = None
depends_on = None


def _column_exists(insp, table: str, column: str) -> bool:
    try:
        return any(c["name"] == column for c in insp.get_columns(table))
    except Exception:  # noqa: BLE001 — defensive on exotic dialects
        return False


def _index_exists(insp, table: str, index: str) -> bool:
    try:
        return any(i["name"] == index for i in insp.get_indexes(table))
    except Exception:  # noqa: BLE001
        return False


def _table_exists(insp, table: str) -> bool:
    try:
        return table in insp.get_table_names()
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    # FK to agents.id is only emitted when the agents table is present.
    # In some test-isolation paths (alembic stamp past Sprint-1 head) the
    # agents table may not yet exist; the new column is added without the
    # FK constraint and the constraint can be added later. In production,
    # agents predates fee_collections so this branch never runs.
    agents_present = _table_exists(insp, "agents")

    if not _table_exists(insp, "fee_collections"):
        # Defensive: if a fresh DB never had the legacy table, create the
        # full Sprint-2 shape directly.
        cols = [
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("source_type", sa.String(50), nullable=False),
            sa.Column("source_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("amount", sa.Numeric(20, 12), nullable=False),
            sa.Column("token", sa.String(20), nullable=False),
            sa.Column("usd_value_at_collection", sa.Numeric(20, 2), nullable=True),
            sa.Column("status", sa.String(20), nullable=True),
            sa.Column("collection_tx_hash", sa.String(255), nullable=True),
            sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        ]
        if agents_present:
            cols.append(sa.Column(
                "payer_agent_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id"),
                nullable=True,
            ))
        else:
            cols.append(sa.Column(
                "payer_agent_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                nullable=True,
            ))
        cols.extend([
            sa.Column("amount_piconero", sa.BigInteger(), nullable=True),
            sa.Column("rate_applied_bps", sa.Integer(), nullable=True),
            sa.Column("transaction_ref", sa.String(255), nullable=True),
            sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        ])
        op.create_table("fee_collections", *cols)
        logger.info("x5y6z7a8b9c0: created fee_collections from scratch")
        insp = sa_inspect(conn)

    # Add new columns idempotently. Each ALTER TABLE is independent.
    if not _column_exists(insp, "fee_collections", "payer_agent_id"):
        if agents_present:
            op.add_column(
                "fee_collections",
                sa.Column(
                    "payer_agent_id",
                    sa.dialects.postgresql.UUID(as_uuid=True),
                    sa.ForeignKey("agents.id"),
                    nullable=True,
                ),
            )
        else:
            op.add_column(
                "fee_collections",
                sa.Column(
                    "payer_agent_id",
                    sa.dialects.postgresql.UUID(as_uuid=True),
                    nullable=True,
                ),
            )
        logger.info("x5y6z7a8b9c0: added fee_collections.payer_agent_id")

    if not _column_exists(insp, "fee_collections", "amount_piconero"):
        op.add_column(
            "fee_collections",
            sa.Column("amount_piconero", sa.BigInteger(), nullable=True),
        )
        logger.info("x5y6z7a8b9c0: added fee_collections.amount_piconero")

    if not _column_exists(insp, "fee_collections", "rate_applied_bps"):
        op.add_column(
            "fee_collections",
            sa.Column("rate_applied_bps", sa.Integer(), nullable=True),
        )
        logger.info("x5y6z7a8b9c0: added fee_collections.rate_applied_bps")

    if not _column_exists(insp, "fee_collections", "transaction_ref"):
        op.add_column(
            "fee_collections",
            sa.Column("transaction_ref", sa.String(255), nullable=True),
        )
        logger.info("x5y6z7a8b9c0: added fee_collections.transaction_ref")

    if not _column_exists(insp, "fee_collections", "collected_at"):
        op.add_column(
            "fee_collections",
            sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        )
        logger.info("x5y6z7a8b9c0: added fee_collections.collected_at")

    insp = sa_inspect(conn)

    if not _index_exists(insp, "fee_collections", "ix_fee_collections_payer_collected"):
        op.create_index(
            "ix_fee_collections_payer_collected",
            "fee_collections",
            ["payer_agent_id", "collected_at"],
        )
        logger.info("x5y6z7a8b9c0: created ix_fee_collections_payer_collected")

    if not _index_exists(insp, "fee_collections", "ix_fee_collections_collected_at"):
        op.create_index(
            "ix_fee_collections_collected_at",
            "fee_collections",
            ["collected_at"],
        )
        logger.info("x5y6z7a8b9c0: created ix_fee_collections_collected_at")


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    if _index_exists(insp, "fee_collections", "ix_fee_collections_collected_at"):
        op.drop_index(
            "ix_fee_collections_collected_at", table_name="fee_collections"
        )
        logger.info("x5y6z7a8b9c0 downgrade: dropped ix_fee_collections_collected_at")

    if _index_exists(insp, "fee_collections", "ix_fee_collections_payer_collected"):
        op.drop_index(
            "ix_fee_collections_payer_collected", table_name="fee_collections"
        )
        logger.info(
            "x5y6z7a8b9c0 downgrade: dropped ix_fee_collections_payer_collected"
        )

    insp = sa_inspect(conn)

    is_sqlite = conn.dialect.name == "sqlite"

    # On SQLite, ``ALTER TABLE DROP COLUMN`` of a column with a foreign-key
    # reference fails because SQLite stores the FK definition by column name
    # in its sqlite_schema metadata. Use batch_alter_table which rebuilds the
    # table without the dropped columns. On Postgres, plain drop_column works.
    if is_sqlite:
        # Provide a copy_from descriptor so batch_alter doesn't need to
        # reflect FK targets (the agents table may have been dropped or
        # not yet created in some test isolation paths).
        from sqlalchemy import Table, MetaData
        meta = MetaData()
        try:
            existing = Table(
                "fee_collections",
                meta,
                autoload_with=conn,
            )
        except Exception:
            existing = None

        with op.batch_alter_table(
            "fee_collections",
            copy_from=existing,
            recreate="always",
        ) as batch_op:
            for col in (
                "collected_at",
                "transaction_ref",
                "rate_applied_bps",
                "amount_piconero",
                "payer_agent_id",
            ):
                if _column_exists(insp, "fee_collections", col):
                    batch_op.drop_column(col)
    else:
        if _column_exists(insp, "fee_collections", "collected_at"):
            op.drop_column("fee_collections", "collected_at")
        if _column_exists(insp, "fee_collections", "transaction_ref"):
            op.drop_column("fee_collections", "transaction_ref")
        if _column_exists(insp, "fee_collections", "rate_applied_bps"):
            op.drop_column("fee_collections", "rate_applied_bps")
        if _column_exists(insp, "fee_collections", "amount_piconero"):
            op.drop_column("fee_collections", "amount_piconero")
        if _column_exists(insp, "fee_collections", "payer_agent_id"):
            op.drop_column("fee_collections", "payer_agent_id")
    logger.info("x5y6z7a8b9c0 downgrade: dropped Sprint-2 fee_collections columns")
