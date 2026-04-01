"""Phase 3c: Multi-currency support -- swap_orders and currency_conversions tables.

Creates:
  - swapstatus enum (PostgreSQL only)
  - swap_orders table
  - currency_conversions table

Revision ID: i0j1k2l3m4n5
Revises: h9i0j1k2l3m4
Create Date: 2026-04-01
"""
from alembic import op
import sqlalchemy as sa

revision = "i0j1k2l3m4n5"
down_revision = "h9i0j1k2l3m4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ------------------------------------------------------------------
    # 1. Create swapstatus enum (PostgreSQL only)
    # ------------------------------------------------------------------
    if is_pg:
        op.execute(
            "DO $$ BEGIN "
            "CREATE TYPE swapstatus AS ENUM "
            "('pending', 'completed', 'failed', 'expired', 'cancelled'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )

    # ------------------------------------------------------------------
    # 2. Create swap_orders table
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS swap_orders (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                from_currency VARCHAR(10) NOT NULL,
                from_amount NUMERIC(20, 8) NOT NULL,
                to_currency VARCHAR(10) NOT NULL,
                to_amount NUMERIC(20, 8),
                rate NUMERIC(20, 8),
                fee_amount NUMERIC(20, 8),
                status swapstatus DEFAULT 'pending',
                external_ref VARCHAR(255),
                created_at TIMESTAMPTZ DEFAULT now(),
                completed_at TIMESTAMPTZ
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_swap_orders_agent_id
            ON swap_orders (agent_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_swap_orders_status
            ON swap_orders (status)
        """)
    else:
        op.execute("""
            CREATE TABLE IF NOT EXISTS swap_orders (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL REFERENCES agents(id),
                from_currency VARCHAR(10) NOT NULL,
                from_amount NUMERIC NOT NULL,
                to_currency VARCHAR(10) NOT NULL,
                to_amount NUMERIC,
                rate NUMERIC,
                fee_amount NUMERIC,
                status VARCHAR(20) DEFAULT 'pending',
                external_ref VARCHAR(255),
                created_at DATETIME,
                completed_at DATETIME
            )
        """)

    # ------------------------------------------------------------------
    # 3. Create currency_conversions table
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS currency_conversions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                from_currency VARCHAR(10) NOT NULL,
                from_amount NUMERIC(20, 8) NOT NULL,
                to_currency VARCHAR(10) NOT NULL,
                to_amount NUMERIC(20, 8) NOT NULL,
                rate NUMERIC(20, 8) NOT NULL,
                fee_amount NUMERIC(20, 8) NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_currency_conversions_agent_id
            ON currency_conversions (agent_id)
        """)
    else:
        op.execute("""
            CREATE TABLE IF NOT EXISTS currency_conversions (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL REFERENCES agents(id),
                from_currency VARCHAR(10) NOT NULL,
                from_amount NUMERIC NOT NULL,
                to_currency VARCHAR(10) NOT NULL,
                to_amount NUMERIC NOT NULL,
                rate NUMERIC NOT NULL,
                fee_amount NUMERIC NOT NULL,
                created_at DATETIME
            )
        """)


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.execute("DROP TABLE IF EXISTS currency_conversions")
    op.execute("DROP TABLE IF EXISTS swap_orders")

    if is_pg:
        op.execute("DROP TYPE IF EXISTS swapstatus")
