"""Phase 2 Sprint 3 (revenue-and-tee): tier_grace_until + agent_monthly_stats.

Revision ID: y6z7a8b9c0d1
Revises: x5y6z7a8b9c0
Create Date: 2026-05-07

Sprint 3 schema additions for subscription tier enforcement:

* ``agents.tier_grace_until`` — TIMESTAMPTZ NULL. When set, tier-limit
  enforcement preserves the agent's declared tier until expiry; after
  expiry, the agent is treated as FREE for limit checks even if billing
  has not yet flipped the ``tier`` column. Sprint 4 wires the billing
  cron that sets this column when an XMR auto-debit fails.

* New table ``agent_monthly_stats`` — per-agent monthly transfer counter
  for FREE-tier 100 tx/month enforcement. Composite PK on
  ``(agent_id, month_start)``. ``record_transaction`` upserts atomically.

* Grandfather invariant — per ``lead-decisions.md`` "ВСЕ grandfathered to
  Free": rows with ``tier IS NULL`` are set to FREE. Existing VERIFIED /
  PREMIUM / ENTERPRISE rows keep their tier.

Idempotent — every ADD / DROP inspects the schema first and skips if the
target object is already present.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger("alembic.env")

# Revision identifiers
revision = "y6z7a8b9c0d1"
down_revision = "x5y6z7a8b9c0"
branch_labels = None
depends_on = None


def _column_exists(insp, table: str, column: str) -> bool:
    try:
        return any(c["name"] == column for c in insp.get_columns(table))
    except Exception:  # noqa: BLE001
        return False


def _index_exists(insp, table: str, index: str) -> bool:
    try:
        return any(i["name"] == index for i in insp.get_indexes(table))
    except Exception:  # noqa: BLE001
        return False


def _table_exists(insp, table: str) -> bool:
    try:
        return table in insp.get_table_names()
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    # 1. Add ``agents.tier_grace_until`` column (idempotent).
    if _table_exists(insp, "agents") and not _column_exists(
        insp, "agents", "tier_grace_until"
    ):
        op.add_column(
            "agents",
            sa.Column(
                "tier_grace_until",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        logger.info("y6z7a8b9c0d1: added agents.tier_grace_until")

    # 2. Grandfather rows with NULL tier to FREE. Existing
    # VERIFIED / PREMIUM / ENTERPRISE rows are untouched.
    if _table_exists(insp, "agents"):
        try:
            conn.execute(
                sa.text(
                    "UPDATE agents SET tier = 'free' WHERE tier IS NULL"
                )
            )
            logger.info("y6z7a8b9c0d1: grandfathered NULL tier rows to FREE")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "y6z7a8b9c0d1: grandfather UPDATE skipped (%s)", exc
            )

    # 3. Create agent_monthly_stats table (idempotent).
    if not _table_exists(insp, "agent_monthly_stats"):
        # FK to agents.id when present; otherwise plain UUID column (test
        # isolation paths may stamp here without agents).
        agents_present = _table_exists(insp, "agents")
        agent_id_col_kwargs = {"nullable": False}
        if agents_present:
            agent_id_fk = sa.ForeignKey("agents.id", ondelete="CASCADE")
            cols = [
                sa.Column(
                    "agent_id",
                    sa.dialects.postgresql.UUID(as_uuid=True),
                    agent_id_fk,
                    **agent_id_col_kwargs,
                ),
            ]
        else:
            cols = [
                sa.Column(
                    "agent_id",
                    sa.dialects.postgresql.UUID(as_uuid=True),
                    **agent_id_col_kwargs,
                ),
            ]
        cols.extend(
            [
                sa.Column("month_start", sa.Date(), nullable=False),
                sa.Column(
                    "transaction_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
                sa.Column(
                    "last_updated",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.func.now(),
                ),
                sa.PrimaryKeyConstraint(
                    "agent_id",
                    "month_start",
                    name="pk_agent_monthly_stats",
                ),
            ]
        )
        op.create_table("agent_monthly_stats", *cols)
        logger.info("y6z7a8b9c0d1: created agent_monthly_stats")

        insp = sa_inspect(conn)
        if not _index_exists(
            insp, "agent_monthly_stats", "ix_agent_monthly_stats_agent_month_desc"
        ):
            op.create_index(
                "ix_agent_monthly_stats_agent_month_desc",
                "agent_monthly_stats",
                ["agent_id", "month_start"],
            )
            logger.info(
                "y6z7a8b9c0d1: created ix_agent_monthly_stats_agent_month_desc"
            )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    if _table_exists(insp, "agent_monthly_stats"):
        if _index_exists(
            insp,
            "agent_monthly_stats",
            "ix_agent_monthly_stats_agent_month_desc",
        ):
            op.drop_index(
                "ix_agent_monthly_stats_agent_month_desc",
                table_name="agent_monthly_stats",
            )
        op.drop_table("agent_monthly_stats")
        logger.info("y6z7a8b9c0d1 downgrade: dropped agent_monthly_stats")

    insp = sa_inspect(conn)
    if _table_exists(insp, "agents") and _column_exists(
        insp, "agents", "tier_grace_until"
    ):
        is_sqlite = conn.dialect.name == "sqlite"
        if is_sqlite:
            from sqlalchemy import Table, MetaData

            meta = MetaData()
            try:
                existing = Table(
                    "agents", meta, autoload_with=conn
                )
            except Exception:  # noqa: BLE001
                existing = None
            with op.batch_alter_table(
                "agents", copy_from=existing, recreate="always"
            ) as batch_op:
                batch_op.drop_column("tier_grace_until")
        else:
            op.drop_column("agents", "tier_grace_until")
        logger.info("y6z7a8b9c0d1 downgrade: dropped agents.tier_grace_until")
