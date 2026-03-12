"""add balance constraints, indexes, and timezone-aware columns

Revision ID: b3c4d5e6f7g8
Revises: a1b2c3d4e5f6
Create Date: 2026-03-11 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7g8'
down_revision: Union[str, Sequence[str]] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Task 5: Add missing CheckConstraints and indexes ---

    op.create_check_constraint(
        'ck_balance_available_non_negative',
        'agent_balances',
        'available >= 0',
    )
    op.create_check_constraint(
        'ck_balance_pending_non_negative',
        'agent_balances',
        'pending >= 0',
    )
    op.create_index(
        'ix_agent_balances_deposit_address',
        'agent_balances',
        ['deposit_address'],
    )
    op.create_index(
        'ix_transactions_created_at',
        'transactions',
        ['created_at'],
    )

    # --- Task 6: Convert timestamp columns to timezone-aware ---

    _tz_tables = {
        'agents': ['created_at', 'updated_at', 'verified_at', 'last_seen_at'],
        'transactions': ['created_at', 'updated_at'],
        'hub_routes': ['created_at', 'confirmed_at', 'settled_at', 'cancelled_at'],
        'webhook_events': ['created_at', 'delivered_at', 'next_retry_at'],
        'escrow_deals': ['created_at', 'updated_at', 'expires_at', 'completed_at', 'cancelled_at'],
        'payment_channels': ['created_at', 'updated_at', 'expires_at', 'closed_at'],
        'fee_collections': ['created_at', 'settled_at'],
        'audit_logs': ['created_at'],
        'system_state': ['updated_at'],
    }

    for table, columns in _tz_tables.items():
        for col in columns:
            try:
                op.alter_column(
                    table, col,
                    type_=sa.DateTime(timezone=True),
                    existing_type=sa.DateTime(),
                )
            except Exception:
                pass  # Column may not exist in all tables


def downgrade() -> None:
    # Revert timezone changes (best-effort)
    _tz_tables = {
        'agents': ['created_at', 'updated_at', 'verified_at', 'last_seen_at'],
        'transactions': ['created_at', 'updated_at'],
        'hub_routes': ['created_at', 'confirmed_at', 'settled_at', 'cancelled_at'],
        'webhook_events': ['created_at', 'delivered_at', 'next_retry_at'],
        'escrow_deals': ['created_at', 'updated_at', 'expires_at', 'completed_at', 'cancelled_at'],
        'payment_channels': ['created_at', 'updated_at', 'expires_at', 'closed_at'],
        'fee_collections': ['created_at', 'settled_at'],
        'audit_logs': ['created_at'],
        'system_state': ['updated_at'],
    }

    for table, columns in _tz_tables.items():
        for col in columns:
            try:
                op.alter_column(
                    table, col,
                    type_=sa.DateTime(),
                    existing_type=sa.DateTime(timezone=True),
                )
            except Exception:
                pass

    op.drop_index('ix_transactions_created_at', 'transactions')
    op.drop_index('ix_agent_balances_deposit_address', 'agent_balances')
    op.drop_constraint('ck_balance_pending_non_negative', 'agent_balances')
    op.drop_constraint('ck_balance_available_non_negative', 'agent_balances')
