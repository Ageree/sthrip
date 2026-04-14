"""Add external_order_id, deposit_address, provider_name to swap_orders.

These columns support the real exchange provider integration (ChangeNOW / SideShift).

Revision ID: k2l3m4n5o6p7
Revises: j1k2l3m4n5o6
Create Date: 2026-04-01
"""
from alembic import op
import sqlalchemy as sa

revision = "k2l3m4n5o6p7"
down_revision = "j1k2l3m4n5o6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # Add external_order_id (VARCHAR 128, nullable, indexed)
    if is_pg:
        op.execute("""
            ALTER TABLE swap_orders
            ADD COLUMN IF NOT EXISTS external_order_id VARCHAR(128)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_swap_orders_external_order_id
            ON swap_orders (external_order_id)
        """)
    else:
        # SQLite does not support ADD COLUMN IF NOT EXISTS — check via pragma
        result = bind.execute(sa.text(
            "SELECT COUNT(*) FROM pragma_table_info('swap_orders') "
            "WHERE name = 'external_order_id'"
        )).scalar()
        if result == 0:
            bind.execute(sa.text(
                "ALTER TABLE swap_orders ADD COLUMN external_order_id VARCHAR(128)"
            ))

    # Add deposit_address (VARCHAR 255, nullable)
    if is_pg:
        op.execute("""
            ALTER TABLE swap_orders
            ADD COLUMN IF NOT EXISTS deposit_address VARCHAR(255)
        """)
    else:
        result = bind.execute(sa.text(
            "SELECT COUNT(*) FROM pragma_table_info('swap_orders') "
            "WHERE name = 'deposit_address'"
        )).scalar()
        if result == 0:
            bind.execute(sa.text(
                "ALTER TABLE swap_orders ADD COLUMN deposit_address VARCHAR(255)"
            ))

    # Add provider_name (VARCHAR 32, nullable)
    if is_pg:
        op.execute("""
            ALTER TABLE swap_orders
            ADD COLUMN IF NOT EXISTS provider_name VARCHAR(32)
        """)
    else:
        result = bind.execute(sa.text(
            "SELECT COUNT(*) FROM pragma_table_info('swap_orders') "
            "WHERE name = 'provider_name'"
        )).scalar()
        if result == 0:
            bind.execute(sa.text(
                "ALTER TABLE swap_orders ADD COLUMN provider_name VARCHAR(32)"
            ))


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("ALTER TABLE swap_orders DROP COLUMN IF EXISTS external_order_id")
        op.execute("ALTER TABLE swap_orders DROP COLUMN IF EXISTS deposit_address")
        op.execute("ALTER TABLE swap_orders DROP COLUMN IF EXISTS provider_name")
    # SQLite does not support DROP COLUMN — downgrade is a no-op on SQLite
