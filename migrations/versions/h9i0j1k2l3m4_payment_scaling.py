"""Phase 3b: Payment Scaling -- channels v2, recurring payments, payment streams.

Adds deposit/balance/nonce/settlement columns to payment_channels, extends
channelstatus enum with 'settled', creates recurringinterval and streamstatus
enums, and creates the channel_updates, recurring_payments, and payment_streams
tables.

Revision ID: h9i0j1k2l3m4
Revises: g8h9i0j1k2l3
Create Date: 2026-04-01
"""
from alembic import op
import sqlalchemy as sa

revision = "h9i0j1k2l3m4"
down_revision = "g8h9i0j1k2l3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ------------------------------------------------------------------
    # 1. Extend payment_channels with deposit/balance/settlement columns
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            ALTER TABLE payment_channels
                ADD COLUMN IF NOT EXISTS deposit_a NUMERIC(20, 12),
                ADD COLUMN IF NOT EXISTS deposit_b NUMERIC(20, 12),
                ADD COLUMN IF NOT EXISTS balance_a NUMERIC(20, 12),
                ADD COLUMN IF NOT EXISTS balance_b NUMERIC(20, 12),
                ADD COLUMN IF NOT EXISTS nonce INTEGER DEFAULT 0,
                ADD COLUMN IF NOT EXISTS last_update_sig_a TEXT,
                ADD COLUMN IF NOT EXISTS last_update_sig_b TEXT,
                ADD COLUMN IF NOT EXISTS settlement_period INTEGER DEFAULT 3600,
                ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS closes_at TIMESTAMPTZ
        """)
    else:
        # SQLite: add columns one at a time (no compound ADD COLUMN support)
        for col_sql in (
            "ALTER TABLE payment_channels ADD COLUMN deposit_a NUMERIC",
            "ALTER TABLE payment_channels ADD COLUMN deposit_b NUMERIC",
            "ALTER TABLE payment_channels ADD COLUMN balance_a NUMERIC",
            "ALTER TABLE payment_channels ADD COLUMN balance_b NUMERIC",
            "ALTER TABLE payment_channels ADD COLUMN nonce INTEGER DEFAULT 0",
            "ALTER TABLE payment_channels ADD COLUMN last_update_sig_a TEXT",
            "ALTER TABLE payment_channels ADD COLUMN last_update_sig_b TEXT",
            "ALTER TABLE payment_channels ADD COLUMN settlement_period INTEGER DEFAULT 3600",
            "ALTER TABLE payment_channels ADD COLUMN settled_at DATETIME",
            "ALTER TABLE payment_channels ADD COLUMN closes_at DATETIME",
        ):
            try:
                op.execute(col_sql)
            except Exception:
                pass  # column already exists -- idempotent

    # ------------------------------------------------------------------
    # 2. Extend channelstatus enum with 'settled' (PostgreSQL only)
    # ------------------------------------------------------------------
    if is_pg:
        op.execute(
            "DO $$ BEGIN "
            "ALTER TYPE channelstatus ADD VALUE IF NOT EXISTS 'settled'; "
            "EXCEPTION WHEN others THEN NULL; END $$"
        )

    # ------------------------------------------------------------------
    # 3. Create recurringinterval and streamstatus enums (PostgreSQL only)
    # ------------------------------------------------------------------
    if is_pg:
        op.execute(
            "DO $$ BEGIN "
            "CREATE TYPE recurringinterval AS ENUM "
            "('hourly', 'daily', 'weekly', 'monthly'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        op.execute(
            "DO $$ BEGIN "
            "CREATE TYPE streamstatus AS ENUM "
            "('active', 'paused', 'stopped', 'expired'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )

    # ------------------------------------------------------------------
    # 4. channel_updates table
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS channel_updates (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                channel_id UUID NOT NULL REFERENCES payment_channels(id) ON DELETE CASCADE,
                nonce INTEGER NOT NULL,
                balance_a NUMERIC(20, 12) NOT NULL,
                balance_b NUMERIC(20, 12) NOT NULL,
                signature_a TEXT NOT NULL,
                signature_b TEXT NOT NULL,
                is_final BOOLEAN DEFAULT false,
                created_at TIMESTAMPTZ DEFAULT now(),
                CONSTRAINT uq_channel_update_nonce UNIQUE (channel_id, nonce)
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_channel_updates_channel_id
            ON channel_updates (channel_id)
        """)
    else:
        op.execute("""
            CREATE TABLE IF NOT EXISTS channel_updates (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL REFERENCES payment_channels(id),
                nonce INTEGER NOT NULL,
                balance_a NUMERIC NOT NULL,
                balance_b NUMERIC NOT NULL,
                signature_a TEXT NOT NULL,
                signature_b TEXT NOT NULL,
                is_final INTEGER DEFAULT 0,
                created_at DATETIME,
                UNIQUE (channel_id, nonce)
            )
        """)

    # ------------------------------------------------------------------
    # 5. recurring_payments table
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS recurring_payments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                payer_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                payee_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                amount NUMERIC(20, 12) NOT NULL,
                currency VARCHAR(10) DEFAULT 'XMR',
                interval_seconds INTEGER NOT NULL,
                max_payments INTEGER,
                payments_made INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT true,
                next_payment_at TIMESTAMPTZ NOT NULL,
                last_payment_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now(),
                cancelled_at TIMESTAMPTZ
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_recurring_payments_payer_id
            ON recurring_payments (payer_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_recurring_payments_payee_id
            ON recurring_payments (payee_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_recurring_payments_next_payment_at
            ON recurring_payments (next_payment_at)
            WHERE is_active = true
        """)
    else:
        op.execute("""
            CREATE TABLE IF NOT EXISTS recurring_payments (
                id TEXT PRIMARY KEY,
                payer_id TEXT NOT NULL REFERENCES agents(id),
                payee_id TEXT NOT NULL REFERENCES agents(id),
                amount NUMERIC NOT NULL,
                currency VARCHAR(10) DEFAULT 'XMR',
                interval_seconds INTEGER NOT NULL,
                max_payments INTEGER,
                payments_made INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                next_payment_at DATETIME NOT NULL,
                last_payment_at DATETIME,
                created_at DATETIME,
                cancelled_at DATETIME
            )
        """)

    # ------------------------------------------------------------------
    # 6. payment_streams table
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS payment_streams (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                channel_id UUID NOT NULL REFERENCES payment_channels(id) ON DELETE CASCADE,
                rate_per_second NUMERIC(20, 12) NOT NULL,
                status streamstatus DEFAULT 'active',
                total_streamed NUMERIC(20, 12) DEFAULT 0,
                started_at TIMESTAMPTZ DEFAULT now(),
                paused_at TIMESTAMPTZ,
                stopped_at TIMESTAMPTZ,
                expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_payment_streams_channel_id
            ON payment_streams (channel_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_payment_streams_status
            ON payment_streams (status)
            WHERE status = 'active'
        """)
    else:
        op.execute("""
            CREATE TABLE IF NOT EXISTS payment_streams (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL REFERENCES payment_channels(id),
                rate_per_second NUMERIC NOT NULL,
                status VARCHAR(20) DEFAULT 'active',
                total_streamed NUMERIC DEFAULT 0,
                started_at DATETIME,
                paused_at DATETIME,
                stopped_at DATETIME,
                expires_at DATETIME,
                created_at DATETIME
            )
        """)


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.execute("DROP TABLE IF EXISTS payment_streams")
    op.execute("DROP TABLE IF EXISTS recurring_payments")
    op.execute("DROP TABLE IF EXISTS channel_updates")

    if is_pg:
        op.execute("DROP TYPE IF EXISTS streamstatus")
        op.execute("DROP TYPE IF EXISTS recurringinterval")
        # Note: cannot remove a value from a PostgreSQL enum, so we do not
        # revert the 'settled' addition to channelstatus.

        # Remove the added columns from payment_channels
        op.execute("""
            ALTER TABLE payment_channels
                DROP COLUMN IF EXISTS deposit_a,
                DROP COLUMN IF EXISTS deposit_b,
                DROP COLUMN IF EXISTS balance_a,
                DROP COLUMN IF EXISTS balance_b,
                DROP COLUMN IF EXISTS nonce,
                DROP COLUMN IF EXISTS last_update_sig_a,
                DROP COLUMN IF EXISTS last_update_sig_b,
                DROP COLUMN IF EXISTS settlement_period,
                DROP COLUMN IF EXISTS settled_at,
                DROP COLUMN IF EXISTS closes_at
        """)
