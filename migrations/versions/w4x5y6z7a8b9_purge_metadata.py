"""Phase 2 Sprint 1 (revenue-and-tee): purge_metadata + canary_state tables.

Revision ID: w4x5y6z7a8b9
Revises: v3w4x5y6z7a8
Create Date: 2026-05-07

Adds two new tables for the auto-purge + warrant canary subsystem:

* ``purge_metadata`` — one row per scheduled purge run; counts deleted rows
  per source table and links to the synthetic audit_log "chain reset" row
  (when the purge swept any audit log entries).
* ``canary_state`` — single-row store (id=1) for the most recently signed
  warrant canary payload + Ed25519 detached signature.

Idempotency: every CREATE / ADD / DROP inspects the schema first so a
re-run is a no-op once the migration has succeeded once. Mirrors the
existing ``IF NOT EXISTS`` discipline used by Sprint 4b.

Downgrade is destructive (drops the two tables). The data is operational
metadata only — no plaintext payment-graph fields — so this is safe.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger("alembic.env")

# Revision identifiers
revision = "w4x5y6z7a8b9"
down_revision = "v3w4x5y6z7a8"
branch_labels = None
depends_on = None


def _table_exists(insp, table: str) -> bool:
    try:
        return table in insp.get_table_names()
    except Exception:  # noqa: BLE001 — defensive on exotic dialects
        return False


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    if not _table_exists(insp, "purge_metadata"):
        op.create_table(
            "purge_metadata",
            sa.Column(
                "id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                primary_key=True,
                autoincrement=True,
                nullable=False,
            ),
            sa.Column(
                "run_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "transactions_deleted",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "escrow_deals_deleted",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "escrow_milestones_deleted",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "message_relays_deleted",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "audit_log_deleted",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            # Stores the audit_log id (UUID stringified) of the synthetic
            # chain-reset row so operators can correlate a purge run with the
            # break-point in the HMAC chain. Nullable: zero audit-log purges
            # leaves it NULL.
            sa.Column("audit_chain_reset_at_id", sa.String(64), nullable=True),
        )
        op.create_index(
            "ix_purge_metadata_run_at_desc",
            "purge_metadata",
            [sa.text("run_at DESC")],
        )
        logger.info("w4x5y6z7a8b9: created purge_metadata")
    else:
        logger.info("w4x5y6z7a8b9: purge_metadata already exists; skipping")

    if not _table_exists(insp, "canary_state"):
        op.create_table(
            "canary_state",
            sa.Column(
                "id",
                sa.Integer(),
                primary_key=True,
                autoincrement=False,
                nullable=False,
            ),
            sa.Column(
                "signed_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "payload_json",
                sa.Text(),
                nullable=False,
            ),
            sa.Column(
                "signature_b64",
                sa.Text(),
                nullable=False,
            ),
        )
        logger.info("w4x5y6z7a8b9: created canary_state")
    else:
        logger.info("w4x5y6z7a8b9: canary_state already exists; skipping")


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    if _table_exists(insp, "canary_state"):
        op.drop_table("canary_state")
        logger.info("w4x5y6z7a8b9 downgrade: dropped canary_state")

    insp = sa_inspect(conn)
    if _table_exists(insp, "purge_metadata"):
        # Drop index first if it exists; SQLite drops it implicitly with the
        # table but Postgres needs an explicit drop only when the index lives
        # outside of the table definition (it does here because we used
        # create_index above).
        try:
            op.drop_index("ix_purge_metadata_run_at_desc", table_name="purge_metadata")
        except Exception:  # noqa: BLE001
            logger.info("w4x5y6z7a8b9 downgrade: index already gone")
        op.drop_table("purge_metadata")
        logger.info("w4x5y6z7a8b9 downgrade: dropped purge_metadata")
