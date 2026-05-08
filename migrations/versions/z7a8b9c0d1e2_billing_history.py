"""Phase 2 Sprint 4 (revenue-and-tee): agent_billing_history table.

Revision ID: z7a8b9c0d1e2
Revises: y6z7a8b9c0d1
Create Date: 2026-05-07

Sprint 4 schema additions for XMR subscription billing:

* New table ``agent_billing_history`` — append-only ledger of every billing
  event (monthly cron charge, grace-start, grace-retry, expiry-downgrade,
  upgrade pro-rated charge, downgrade pro-rated refund). Pinned columns:

    - ``id`` BIGINT PK (autoincrement)
    - ``agent_id`` UUID FK -> agents.id  ON DELETE CASCADE
    - ``month_start`` DATE NOT NULL — UTC first-of-month for monthly cron
      rows; for upgrade/downgrade rows we use the calendar month of the
      event so the ``(agent_id, month_start, status)`` partial unique
      constraint guards against double-runs of the cron.
    - ``amount_usd`` NUMERIC(12,2) NOT NULL
    - ``amount_piconero`` BIGINT NOT NULL — XMR equivalent at the time
    - ``rate_applied`` NUMERIC(20,8) NOT NULL — XMR/USD rate used
    - ``status`` VARCHAR(64) NOT NULL — ``monthly_charge``,
      ``monthly_grace_started``, ``monthly_grace_retry``,
      ``monthly_grace_expired_downgrade``, ``upgrade_charge``,
      ``downgrade_refund``, ``monthly_failure_alerted``
    - ``tier_at_event`` VARCHAR(32) NOT NULL
    - ``created_at`` TIMESTAMPTZ NOT NULL DEFAULT now()

* Indexes:

    - ``ix_agent_billing_history_agent_created`` on ``(agent_id, created_at)``
    - ``ix_agent_billing_history_status_created`` on ``(status, created_at)``
    - ``uq_agent_billing_monthly_charge`` UNIQUE on
      ``(agent_id, month_start)`` filtered by ``status='monthly_charge'``
      (Postgres partial). On SQLite a regular composite index is used; the
      service layer enforces the same uniqueness with a SELECT-then-INSERT
      branch guarded by row-level locking. Tests cover both branches.

Idempotent — every CREATE inspects the schema first and skips if the
target object is already present.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger("alembic.env")

# Revision identifiers
revision = "z7a8b9c0d1e2"
down_revision = "y6z7a8b9c0d1"
branch_labels = None
depends_on = None


def _table_exists(insp, table: str) -> bool:
    try:
        return table in insp.get_table_names()
    except Exception:  # noqa: BLE001
        return False


def _index_exists(insp, table: str, index: str) -> bool:
    try:
        return any(i["name"] == index for i in insp.get_indexes(table))
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)
    is_postgres = conn.dialect.name == "postgresql"

    if _table_exists(insp, "agent_billing_history"):
        return

    agents_present = _table_exists(insp, "agents")

    cols = []
    if agents_present:
        cols.append(
            sa.Column(
                "agent_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="CASCADE"),
                nullable=False,
            )
        )
    else:
        cols.append(
            sa.Column(
                "agent_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                nullable=False,
            )
        )

    cols.extend(
        [
            sa.Column(
                "id",
                sa.BigInteger(),
                primary_key=True,
                autoincrement=True,
                nullable=False,
            ),
            sa.Column("month_start", sa.Date(), nullable=False),
            sa.Column("amount_usd", sa.Numeric(12, 2), nullable=False),
            sa.Column("amount_piconero", sa.BigInteger(), nullable=False),
            sa.Column("rate_applied", sa.Numeric(20, 8), nullable=False),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("tier_at_event", sa.String(32), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        ]
    )

    op.create_table("agent_billing_history", *cols)
    logger.info("z7a8b9c0d1e2: created agent_billing_history")

    insp = sa_inspect(conn)
    if not _index_exists(
        insp, "agent_billing_history", "ix_agent_billing_history_agent_created"
    ):
        op.create_index(
            "ix_agent_billing_history_agent_created",
            "agent_billing_history",
            ["agent_id", "created_at"],
        )
    if not _index_exists(
        insp, "agent_billing_history", "ix_agent_billing_history_status_created"
    ):
        op.create_index(
            "ix_agent_billing_history_status_created",
            "agent_billing_history",
            ["status", "created_at"],
        )

    if is_postgres:
        # Partial unique constraint enforces "one monthly_charge row per
        # (agent_id, month_start)" — the cron-idempotency anchor.
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_agent_billing_monthly_charge "
            "ON agent_billing_history (agent_id, month_start) "
            "WHERE status = 'monthly_charge'"
        )
    else:
        # SQLite: regular composite index. Service layer enforces uniqueness
        # via a guarded SELECT inside the same transaction; the index keeps
        # that lookup O(log n).
        if not _index_exists(
            insp,
            "agent_billing_history",
            "ix_agent_billing_monthly_charge_sqlite",
        ):
            op.create_index(
                "ix_agent_billing_monthly_charge_sqlite",
                "agent_billing_history",
                ["agent_id", "month_start", "status"],
            )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)
    is_postgres = conn.dialect.name == "postgresql"

    if not _table_exists(insp, "agent_billing_history"):
        return

    if is_postgres:
        op.execute(
            "DROP INDEX IF EXISTS uq_agent_billing_monthly_charge"
        )

    for ix in (
        "ix_agent_billing_history_agent_created",
        "ix_agent_billing_history_status_created",
        "ix_agent_billing_monthly_charge_sqlite",
    ):
        if _index_exists(insp, "agent_billing_history", ix):
            op.drop_index(ix, table_name="agent_billing_history")

    op.drop_table("agent_billing_history")
    logger.info("z7a8b9c0d1e2 downgrade: dropped agent_billing_history")
