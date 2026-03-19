"""Escrow hub-held model: drop multisig/arbiter fields, add timeout/deadline fields.

Revision ID: c4e5f6a7b8c9
Revises: a1b2c3d4e5f6
Create Date: 2026-03-19

"""
from alembic import op
import sqlalchemy as sa

revision = "c4e5f6a7b8c9"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        # --- Enum migration (PostgreSQL) ---
        # Clean up any leftover type from a failed prior attempt
        op.execute("DROP TYPE IF EXISTS escrowstatus_new")
        op.execute(
            "CREATE TYPE escrowstatus_new AS ENUM "
            "('created', 'accepted', 'delivered', 'completed', 'cancelled', 'expired')"
        )
        op.execute("UPDATE escrow_deals SET status = 'created' WHERE status = 'pending'")
        op.execute(
            "DELETE FROM escrow_deals WHERE status IN ('funded', 'disputed', 'refunded')"
        )
        op.execute(
            "ALTER TABLE escrow_deals "
            "ALTER COLUMN status TYPE VARCHAR USING status::text"
        )
        op.execute("DROP TYPE IF EXISTS escrowstatus")
        op.execute("ALTER TYPE escrowstatus_new RENAME TO escrowstatus")
        op.execute(
            "ALTER TABLE escrow_deals "
            "ALTER COLUMN status TYPE escrowstatus USING status::escrowstatus"
        )
        op.execute(
            "ALTER TABLE escrow_deals "
            "ALTER COLUMN status SET DEFAULT 'created'"
        )

        # --- Drop columns with CASCADE to remove FK constraints/indexes ---
        # Using raw SQL with CASCADE handles FK constraints automatically
        _drop_cols = [
            "arbiter_id",
            "arbiter_fee_percent",
            "arbiter_fee_amount",
            "arbiter_decision",
            "arbiter_signature",
            "disputed_by",
            "disputed_at",
            "dispute_reason",
            "multisig_address",
            "deposit_tx_hash",
            "release_tx_hash",
            "funded_at",
            "timeout_hours",
            "platform_fee_percent",
            "platform_fee_amount",
        ]
        for col in _drop_cols:
            op.execute(
                f"ALTER TABLE escrow_deals DROP COLUMN IF EXISTS {col} CASCADE"
            )
    else:
        # SQLite: just drop columns (no FK/enum issues)
        for col in [
            "arbiter_id", "arbiter_fee_percent", "arbiter_fee_amount",
            "arbiter_decision", "arbiter_signature", "disputed_by",
            "disputed_at", "dispute_reason", "multisig_address",
            "deposit_tx_hash", "release_tx_hash", "funded_at",
            "timeout_hours", "platform_fee_percent", "platform_fee_amount",
        ]:
            try:
                op.drop_column("escrow_deals", col)
            except Exception:
                pass

    # --- Add new columns (idempotent via IF NOT EXISTS for PostgreSQL) ---
    if is_pg:
        _new_cols = [
            ("fee_percent", "NUMERIC(5,4) DEFAULT 0.001"),
            ("fee_amount", "NUMERIC(20,12) DEFAULT 0"),
            ("accept_timeout_hours", "INTEGER DEFAULT 24"),
            ("delivery_timeout_hours", "INTEGER DEFAULT 48"),
            ("review_timeout_hours", "INTEGER DEFAULT 24"),
            ("accept_deadline", "TIMESTAMPTZ"),
            ("delivery_deadline", "TIMESTAMPTZ"),
            ("review_deadline", "TIMESTAMPTZ"),
            ("accepted_at", "TIMESTAMPTZ"),
            ("delivered_at", "TIMESTAMPTZ"),
            ("release_amount", "NUMERIC(20,12)"),
            ("cancelled_at", "TIMESTAMPTZ"),
        ]
        for col_name, col_type in _new_cols:
            op.execute(
                f"ALTER TABLE escrow_deals ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
            )
    else:
        op.add_column("escrow_deals", sa.Column("fee_percent", sa.Numeric(5, 4), server_default="0.001", nullable=True))
        op.add_column("escrow_deals", sa.Column("fee_amount", sa.Numeric(20, 12), server_default="0", nullable=True))
        op.add_column("escrow_deals", sa.Column("accept_timeout_hours", sa.Integer(), server_default="24", nullable=True))
        op.add_column("escrow_deals", sa.Column("delivery_timeout_hours", sa.Integer(), server_default="48", nullable=True))
        op.add_column("escrow_deals", sa.Column("review_timeout_hours", sa.Integer(), server_default="24", nullable=True))
        op.add_column("escrow_deals", sa.Column("accept_deadline", sa.DateTime(timezone=True), nullable=True))
        op.add_column("escrow_deals", sa.Column("delivery_deadline", sa.DateTime(timezone=True), nullable=True))
        op.add_column("escrow_deals", sa.Column("review_deadline", sa.DateTime(timezone=True), nullable=True))
        op.add_column("escrow_deals", sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("escrow_deals", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("escrow_deals", sa.Column("release_amount", sa.Numeric(20, 12), nullable=True))
        op.add_column("escrow_deals", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))

    # Index for the background auto-resolution task
    if is_pg:
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_escrow_deals_status_expires "
            "ON escrow_deals (status, expires_at)"
        )
    else:
        op.create_index(
            "ix_escrow_deals_status_expires",
            "escrow_deals",
            ["status", "expires_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_escrow_deals_status_expires", table_name="escrow_deals")

    for col in [
        "fee_percent", "fee_amount",
        "accept_timeout_hours", "delivery_timeout_hours", "review_timeout_hours",
        "accept_deadline", "delivery_deadline", "review_deadline",
        "accepted_at", "delivered_at", "release_amount", "cancelled_at",
    ]:
        op.drop_column("escrow_deals", col)
