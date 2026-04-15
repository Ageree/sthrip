"""fix pending_withdrawals status column to use withdrawalstatus enum

Fixes installs where the fallback _ensure_pending_withdrawals() created
the status column as VARCHAR(32) instead of the withdrawalstatus enum.

Revision ID: o6p7q8r9s0t1
Revises: n5o6p7q8r9s0
"""
from alembic import op
import sqlalchemy as sa

revision = 'o6p7q8r9s0t1'
down_revision = 'n5o6p7q8r9s0'
branch_labels = None
depends_on = None


def upgrade():
    # Ensure the enum type exists
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE withdrawalstatus AS ENUM ('PENDING', 'COMPLETED', 'FAILED', 'NEEDS_REVIEW');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)

    # Check if the column is currently VARCHAR and convert to enum if so.
    # If it's already using the enum type, this is a safe no-op.
    op.execute("""
        DO $$
        DECLARE
            col_type text;
        BEGIN
            SELECT data_type INTO col_type
            FROM information_schema.columns
            WHERE table_name = 'pending_withdrawals' AND column_name = 'status';

            IF col_type = 'character varying' THEN
                -- Convert existing lowercase values to uppercase for enum compatibility
                UPDATE pending_withdrawals SET status = UPPER(status)
                WHERE status != UPPER(status);

                ALTER TABLE pending_withdrawals
                    ALTER COLUMN status TYPE withdrawalstatus
                    USING status::withdrawalstatus;

                ALTER TABLE pending_withdrawals
                    ALTER COLUMN status SET DEFAULT 'PENDING'::withdrawalstatus;
            END IF;
        END $$;
    """)


def downgrade():
    # Convert back to VARCHAR if needed
    op.execute("""
        DO $$
        DECLARE
            col_type text;
        BEGIN
            SELECT udt_name INTO col_type
            FROM information_schema.columns
            WHERE table_name = 'pending_withdrawals' AND column_name = 'status';

            IF col_type = 'withdrawalstatus' THEN
                ALTER TABLE pending_withdrawals
                    ALTER COLUMN status TYPE VARCHAR(32)
                    USING status::text;

                ALTER TABLE pending_withdrawals
                    ALTER COLUMN status SET DEFAULT 'PENDING';
            END IF;
        END $$;
    """)
