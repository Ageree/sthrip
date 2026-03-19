"""Multi-milestone escrow: new milestones table + deal columns.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-03-19
"""
from alembic import op
import sqlalchemy as sa

revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        # Add PARTIALLY_COMPLETED to escrow status enum
        op.execute("ALTER TYPE escrowstatus ADD VALUE IF NOT EXISTS 'partially_completed'")

        # Create milestone status enum
        op.execute(
            "DO $$ BEGIN "
            "CREATE TYPE milestonestatus AS ENUM "
            "('pending', 'active', 'delivered', 'completed', 'expired', 'cancelled'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )

    # Create escrow_milestones table
    op.create_table(
        "escrow_milestones",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True) if is_pg else sa.String(36), primary_key=True),
        sa.Column("escrow_id", sa.dialects.postgresql.UUID(as_uuid=True) if is_pg else sa.String(36),
                  sa.ForeignKey("escrow_deals.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 12), nullable=False),
        sa.Column("delivery_timeout_hours", sa.Integer(), nullable=False),
        sa.Column("review_timeout_hours", sa.Integer(), nullable=False),
        sa.Column("delivery_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_amount", sa.Numeric(20, 12), nullable=True),
        sa.Column("fee_amount", sa.Numeric(20, 12), server_default="0"),
        sa.Column("status", sa.Enum("pending", "active", "delivered", "completed", "expired", "cancelled",
                                     name="milestonestatus", create_type=False) if is_pg
                  else sa.String(20), server_default="pending"),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("escrow_id", "sequence", name="uq_milestone_sequence"),
        sa.CheckConstraint("sequence >= 1 AND sequence <= 10", name="ck_milestone_sequence_range"),
        sa.CheckConstraint("amount > 0", name="ck_milestone_amount_positive"),
    )

    # Add new columns to escrow_deals
    if is_pg:
        _new_cols = [
            ("is_multi_milestone", "BOOLEAN DEFAULT FALSE"),
            ("milestone_count", "INTEGER DEFAULT 1"),
            ("current_milestone", "INTEGER DEFAULT 1"),
            ("total_released", "NUMERIC(20,12) DEFAULT 0"),
            ("total_fees", "NUMERIC(20,12) DEFAULT 0"),
        ]
        for col_name, col_type in _new_cols:
            op.execute(
                f"ALTER TABLE escrow_deals ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
            )
    else:
        op.add_column("escrow_deals", sa.Column("is_multi_milestone", sa.Boolean(), server_default="0"))
        op.add_column("escrow_deals", sa.Column("milestone_count", sa.Integer(), server_default="1"))
        op.add_column("escrow_deals", sa.Column("current_milestone", sa.Integer(), server_default="1"))
        op.add_column("escrow_deals", sa.Column("total_released", sa.Numeric(20, 12), server_default="0"))
        op.add_column("escrow_deals", sa.Column("total_fees", sa.Numeric(20, 12), server_default="0"))

    # Index for milestone auto-resolution
    if is_pg:
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_escrow_milestones_status_expires "
            "ON escrow_milestones (status, expires_at)"
        )
    else:
        op.create_index("ix_escrow_milestones_status_expires", "escrow_milestones", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_escrow_milestones_status_expires", table_name="escrow_milestones")
    op.drop_table("escrow_milestones")
    for col in ["is_multi_milestone", "milestone_count", "current_milestone", "total_released", "total_fees"]:
        op.drop_column("escrow_deals", col)
