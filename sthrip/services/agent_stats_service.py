"""Phase 2 Sprint 3 — per-agent monthly transaction counter.

Drives FREE-tier 100 tx/month enforcement. ``record_transaction`` is the
single point that increments the counter; it is called from the commission
write path (``TransactionRepository.create_with_commission``) AFTER the fee
row insert and BEFORE commit, sharing the surrounding transaction so a
rolled-back transfer never bumps the counter.

Atomicity:

* PostgreSQL: ``INSERT ... ON CONFLICT (agent_id, month_start) DO UPDATE
  SET transaction_count = agent_monthly_stats.transaction_count + 1``.
* SQLite (tests): ``INSERT OR IGNORE`` of a zero-count seed row, then a
  guarded UPDATE — equivalent atomicity within the single test connection.

Both branches are race-tolerant under contention; we never use
SELECT-then-UPDATE which would lose increments.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional, Union
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from sthrip.db.models import AgentMonthlyStats


def _uuid_text(sql: str) -> "text":
    """Build a TextClause with ``agent_id`` typed as PG UUID for cross-dialect binding.

    SQLAlchemy's PG ``UUID(as_uuid=True)`` knows how to serialize a Python
    ``uuid.UUID`` to the same on-disk representation the ORM uses on both
    Postgres (native uuid) and SQLite (hex, no dashes). Without typed
    bindparams a raw ``str(uuid)`` would land on SQLite as the dashed form
    which then mismatches the ORM-typed reads.
    """
    return text(sql).bindparams(
        bindparam("agent_id", type_=PG_UUID(as_uuid=True))
    )


def _current_month_start(now: Optional[datetime] = None) -> date:
    """First day of the current UTC month as a ``date``."""
    if now is None:
        now = datetime.now(timezone.utc)
    return date(now.year, now.month, 1)


def _coerce_agent_id(agent_id: Union[UUID, str]) -> UUID:
    if isinstance(agent_id, UUID):
        return agent_id
    return UUID(str(agent_id))


def record_transaction(
    agent_id: Union[UUID, str],
    db: Session,
    *,
    now: Optional[datetime] = None,
) -> None:
    """Increment the agent's monthly counter atomically.

    Idempotent under contention: two concurrent calls produce exactly two
    increments. Uses native upsert primitives:

    * On Postgres: ``INSERT ... ON CONFLICT DO UPDATE``.
    * On SQLite: ``INSERT OR IGNORE`` + ``UPDATE`` (equivalent atomicity in
      the single-connection test environment).

    The write joins the caller's surrounding transaction — a rolled back
    transfer never bumps the counter.
    """
    agent_uuid = _coerce_agent_id(agent_id)
    month = _current_month_start(now)
    now_ts = now or datetime.now(timezone.utc)

    bind = db.get_bind()
    dialect = bind.dialect.name if bind is not None else ""

    params = {
        "agent_id": agent_uuid,
        "month_start": month,
        "now_ts": now_ts,
    }

    if dialect == "postgresql":
        # Native ON CONFLICT keeps the increment atomic on Postgres.
        db.execute(
            _uuid_text(
                """
                INSERT INTO agent_monthly_stats
                    (agent_id, month_start, transaction_count, last_updated)
                VALUES
                    (:agent_id, :month_start, 1, :now_ts)
                ON CONFLICT (agent_id, month_start)
                DO UPDATE SET
                    transaction_count = agent_monthly_stats.transaction_count + 1,
                    last_updated = EXCLUDED.last_updated
                """
            ),
            params,
        )
        return

    # SQLite (tests) and other dialects: insert-or-ignore a zero-count seed
    # row, then guard a +1 UPDATE. Both statements run inside the caller's
    # transaction so partial work is rolled back together with the transfer.
    db.execute(
        _uuid_text(
            "INSERT OR IGNORE INTO agent_monthly_stats "
            "(agent_id, month_start, transaction_count, last_updated) "
            "VALUES (:agent_id, :month_start, 0, :now_ts)"
        ),
        params,
    )
    db.execute(
        _uuid_text(
            """
            UPDATE agent_monthly_stats
            SET transaction_count = transaction_count + 1,
                last_updated = :now_ts
            WHERE agent_id = :agent_id AND month_start = :month_start
            """
        ),
        params,
    )


def get_current_month_count(
    agent_id: Union[UUID, str],
    db: Session,
    *,
    now: Optional[datetime] = None,
) -> int:
    """Return the current UTC month's transfer count for ``agent_id``.

    Returns 0 when no row exists for the current month (auto rollover).
    """
    agent_uuid = _coerce_agent_id(agent_id)
    month = _current_month_start(now)
    row = (
        db.query(AgentMonthlyStats.transaction_count)
        .filter(
            AgentMonthlyStats.agent_id == agent_uuid,
            AgentMonthlyStats.month_start == month,
        )
        .first()
    )
    if row is None:
        return 0
    return int(row[0] or 0)


def reset_for_month(month_start: date, db: Session) -> int:
    """Delete all stat rows for ``month_start``. Returns rows deleted.

    Future Sprint 4 cron will call this for archival / rollover; not wired
    in Sprint 3.
    """
    deleted = (
        db.query(AgentMonthlyStats)
        .filter(AgentMonthlyStats.month_start == month_start)
        .delete(synchronize_session=False)
    )
    return int(deleted)


__all__ = [
    "record_transaction",
    "get_current_month_count",
    "reset_for_month",
]
