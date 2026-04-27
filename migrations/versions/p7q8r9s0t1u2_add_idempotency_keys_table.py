"""Add idempotency_keys table for DB-backed idempotency (F-4 fix retry).

Revision ID: p7q8r9s0t1u2
Revises: o6p7q8r9s0t1
Create Date: 2026-04-27

Security fix F-4: idempotency keys previously lived only in Redis with a
24-hour TTL. After TTL expiry a replayed request was processed as new,
risking double-charge.

This migration adds a PostgreSQL-backed authoritative store with no expiry.
Redis is retained as a 1-hour write-through hot-path cache.

UUID column
-----------
The `id` column uses the PostgreSQL native UUID type on Postgres and
VARCHAR(36) on SQLite (for test compatibility). The SQLAlchemy model uses
the same dialect-aware approach via TypeDecorator.

Retention
---------
Rows older than idempotency_db_retention_days (default 90 d) may be purged
by a maintenance cron. The cleanup job is out of scope for this migration;
see sthrip/config.py idempotency_db_retention_days for the configurable
threshold.

SQLite compatibility
--------------------
All DDL uses IF NOT EXISTS / IF EXISTS idioms matching the project convention.
The UUID type is branched per dialect inside upgrade().
"""
import sqlalchemy as sa
from alembic import op

revision = 'p7q8r9s0t1u2'
down_revision = 'o6p7q8r9s0t1'
branch_labels = None
depends_on = None


def _is_sqlite() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "sqlite"


def upgrade():
    # Use native UUID on Postgres; VARCHAR(36) on SQLite (tests).
    if _is_sqlite():
        id_type = "VARCHAR(36)"
    else:
        id_type = "UUID"

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            id          {id_type} NOT NULL,
            agent_id    VARCHAR(255) NOT NULL,
            endpoint    VARCHAR(255) NOT NULL,
            key         VARCHAR(512) NOT NULL,
            request_hash VARCHAR(64) NOT NULL,
            response_status INTEGER NOT NULL DEFAULT 200,
            response_body   TEXT NOT NULL,
            created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_idempotency_keys PRIMARY KEY (id),
            CONSTRAINT uq_idempotency_agent_endpoint_key
                UNIQUE (agent_id, endpoint, key)
        )
    """)

    # Index for retention-based cleanup queries
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_idempotency_keys_created_at
            ON idempotency_keys (created_at)
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_idempotency_keys_created_at")
    op.execute("DROP TABLE IF EXISTS idempotency_keys")
