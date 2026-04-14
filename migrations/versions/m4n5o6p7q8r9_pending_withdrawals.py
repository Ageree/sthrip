"""create pending_withdrawals table

Revision ID: m4n5o6p7q8r9
Revises: l3m4n5o6p7q8
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = 'm4n5o6p7q8r9'
down_revision = 'l3m4n5o6p7q8'
branch_labels = None
depends_on = None


def upgrade():
    # Create enum if not exists
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE withdrawalstatus AS ENUM ('PENDING', 'COMPLETED', 'FAILED', 'NEEDS_REVIEW');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)

    op.create_table(
        'pending_withdrawals',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('agent_id', UUID(as_uuid=True), sa.ForeignKey('agents.id'), nullable=False, index=True),
        sa.Column('amount', sa.Numeric(18, 12), nullable=False),
        sa.Column('address', sa.String(256), nullable=False),
        sa.Column('status', sa.Enum('PENDING', 'COMPLETED', 'FAILED', 'NEEDS_REVIEW', name='withdrawalstatus'), nullable=False),
        sa.Column('tx_hash', sa.String(128), nullable=True),
        sa.Column('error', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index('ix_pending_withdrawals_status_created', 'pending_withdrawals', ['status', 'created_at'])


def downgrade():
    op.drop_index('ix_pending_withdrawals_status_created')
    op.drop_table('pending_withdrawals')
    op.execute('DROP TYPE IF EXISTS withdrawalstatus')
