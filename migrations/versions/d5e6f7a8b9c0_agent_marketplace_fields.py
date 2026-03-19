"""Add marketplace fields to agents: capabilities, pricing, description, accepts_escrow.

Revision ID: d5e6f7a8b9c0
Revises: c4e5f6a7b8c9
Create Date: 2026-03-19

"""
from alembic import op
import sqlalchemy as sa

revision = "d5e6f7a8b9c0"
down_revision = "c4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        _new_cols = [
            ("capabilities", "JSONB DEFAULT '[]'::jsonb"),
            ("pricing", "JSONB DEFAULT '{}'::jsonb"),
            ("description", "TEXT"),
            ("accepts_escrow", "BOOLEAN DEFAULT true"),
        ]
        for col_name, col_type in _new_cols:
            op.execute(
                f"ALTER TABLE agents ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
            )

        # GIN index on capabilities for fast JSON containment queries
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_agents_capabilities "
            "ON agents USING gin (capabilities)"
        )
    else:
        # SQLite fallback
        op.add_column(
            "agents",
            sa.Column("capabilities", sa.JSON(), server_default="[]", nullable=True),
        )
        op.add_column(
            "agents",
            sa.Column("pricing", sa.JSON(), server_default="{}", nullable=True),
        )
        op.add_column(
            "agents",
            sa.Column("description", sa.Text(), nullable=True),
        )
        op.add_column(
            "agents",
            sa.Column("accepts_escrow", sa.Boolean(), server_default="1", nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("DROP INDEX IF EXISTS ix_agents_capabilities")

    for col in ["capabilities", "pricing", "description", "accepts_escrow"]:
        op.drop_column("agents", col)
