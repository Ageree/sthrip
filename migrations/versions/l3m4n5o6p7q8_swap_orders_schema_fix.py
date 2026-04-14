"""Fix swap_orders schema: rename columns to match SQLAlchemy model.

Revision ID: l3m4n5o6p7q8
Revises: k2l3m4n5o6p7
Create Date: 2026-04-01 18:00:00.000000

The original migration (i0j1k2l3m4n5) created swap_orders with
different column names than the SQLAlchemy model expects.  This
migration renames them and adds the missing columns.

Production table has:
  agent_id, rate, status, external_ref, completed_at

Model expects:
  from_agent_id, exchange_rate, state, htlc_hash, htlc_secret,
  btc_tx_hash, xmr_tx_hash, lock_expiry
"""

import sqlalchemy as sa
from alembic import op

revision = "l3m4n5o6p7q8"
down_revision = "k2l3m4n5o6p7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        # ---------------------------------------------------------------
        # 1. Rename existing columns (IF they still have the old name)
        # ---------------------------------------------------------------

        # agent_id -> from_agent_id
        has_old = bind.execute(sa.text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = 'swap_orders' AND column_name = 'agent_id'"
        )).scalar()
        if has_old:
            op.execute("ALTER TABLE swap_orders RENAME COLUMN agent_id TO from_agent_id")

        # rate -> exchange_rate
        has_old = bind.execute(sa.text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = 'swap_orders' AND column_name = 'rate'"
        )).scalar()
        if has_old:
            op.execute("ALTER TABLE swap_orders RENAME COLUMN rate TO exchange_rate")

        # status -> state
        has_old = bind.execute(sa.text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = 'swap_orders' AND column_name = 'status'"
        )).scalar()
        if has_old:
            op.execute("ALTER TABLE swap_orders RENAME COLUMN status TO state")

        # ---------------------------------------------------------------
        # 2. Add missing columns (idempotent)
        # ---------------------------------------------------------------
        for col_name, col_def in [
            ("htlc_hash", "VARCHAR(64)"),
            ("htlc_secret", "VARCHAR(64)"),
            ("btc_tx_hash", "VARCHAR(64)"),
            ("xmr_tx_hash", "VARCHAR(64)"),
            ("lock_expiry", "TIMESTAMPTZ"),
        ]:
            exists = bind.execute(sa.text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name = 'swap_orders' AND column_name = :col"
            ), {"col": col_name}).scalar()
            if not exists:
                op.execute(f"ALTER TABLE swap_orders ADD COLUMN {col_name} {col_def}")

        # ---------------------------------------------------------------
        # 3. Update state enum values: old migration used 'pending',
        #    model expects 'CREATED'/'LOCKED'/etc.  Cast column to VARCHAR
        #    first, update values, then ensure the enum type is correct.
        # ---------------------------------------------------------------
        # Change column type from swapstatus enum to VARCHAR temporarily
        op.execute(
            "ALTER TABLE swap_orders ALTER COLUMN state TYPE VARCHAR(20) "
            "USING state::text"
        )
        # Map old enum values to new ones
        op.execute("UPDATE swap_orders SET state = 'CREATED' WHERE state = 'pending'")
        op.execute("UPDATE swap_orders SET state = 'COMPLETED' WHERE state = 'completed'")
        op.execute("UPDATE swap_orders SET state = 'EXPIRED' WHERE state = 'expired'")
        op.execute("UPDATE swap_orders SET state = 'REFUNDED' WHERE state = 'refunded'")

        # ---------------------------------------------------------------
        # 4. Fix indexes that reference old column names
        # ---------------------------------------------------------------
        op.execute("DROP INDEX IF EXISTS ix_swap_orders_agent_id")
        op.execute("DROP INDEX IF EXISTS ix_swap_orders_status")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_swap_orders_from_agent_id "
            "ON swap_orders (from_agent_id)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_swap_orders_state "
            "ON swap_orders (state)"
        )

    else:
        # SQLite: recreate is simpler but for tests the model auto-creates
        # tables correctly, so this is a no-op for SQLite.
        pass


def downgrade() -> None:
    # Downgrade not supported — this is a one-way schema fix
    pass
