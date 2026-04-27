"""Add tamper-evident HMAC chain columns to audit_log (F-11).

Revision ID: o6p7q8r9s0t1
Revises: n5o6p7q8r9s0
Create Date: 2026-04-27

Migration steps
---------------
1. Add prev_hmac and entry_hmac columns as nullable VARCHAR(64).
2. Add a created_at index for monotonic ordering (used by verify_chain).
3. Backfill existing rows in insertion order (created_at, id) — each row's
   entry_hmac is computed from AUDIT_HMAC_KEY read from the environment, and
   prev_hmac is the previous row's entry_hmac (genesis sentinel for the first
   row).
4. Add UNIQUE index on entry_hmac.

IMPORTANT: Backfill note
------------------------
Pre-migration rows are given computed HMACs, but any tampering that occurred
BEFORE this migration cannot be detected — the chain establishes integrity
from this point forward only.  This limitation is documented in the
audit_logger module docstring.

If AUDIT_HMAC_KEY is not set in the environment when this migration runs,
backfill is skipped and a WARNING is emitted.  Columns remain nullable and
the chain will start from the first post-migration row.

Idempotency
-----------
Column additions use IF NOT EXISTS semantics via SQLAlchemy's inspect-based
guards.  The unique index creation uses IF NOT EXISTS.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger("alembic.env")

# Revision identifiers
revision = "o6p7q8r9s0t1"
down_revision = "n5o6p7q8r9s0"
branch_labels = None
depends_on = None

_GENESIS_HMAC: str = hashlib.sha256(b"genesis").hexdigest()


def _canonical_json(obj) -> str:
    if obj is None:
        return "null"
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _ts_iso(dt: Optional[datetime]) -> str:
    """Canonical naive-UTC ISO string for HMAC (strips tzinfo for consistency)."""
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.isoformat()


def _compute_hmac(key: str, prev_hmac: str, action: str, agent_id: str,
                  ip: str, ts_iso: str, details_json: str) -> str:
    message = "\x00".join([prev_hmac, action, agent_id, ip, ts_iso, details_json])
    return _hmac.new(
        key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)
    existing_cols = {col["name"] for col in insp.get_columns("audit_log")}

    # Step 1: Add new columns if they don't already exist (idempotent).
    if "prev_hmac" not in existing_cols:
        op.add_column("audit_log", sa.Column("prev_hmac", sa.String(64), nullable=True))

    if "entry_hmac" not in existing_cols:
        op.add_column("audit_log", sa.Column("entry_hmac", sa.String(64), nullable=True))

    # Step 2: Add created_at index for monotonic ordering (IF NOT EXISTS).
    existing_indexes = {idx["name"] for idx in insp.get_indexes("audit_log")}
    if "ix_audit_log_created_at" not in existing_indexes:
        op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    # Step 3: Backfill existing rows.
    audit_hmac_key = os.environ.get("AUDIT_HMAC_KEY", "")
    if not audit_hmac_key:
        logger.warning(
            "AUDIT_HMAC_KEY not set — skipping audit_log HMAC backfill. "
            "Existing rows will have NULL prev_hmac/entry_hmac. "
            "The chain will start from the next inserted row."
        )
    else:
        # Fetch all existing rows that haven't been backfilled yet, ordered by
        # insertion time.  We use a raw SQL query for portability.
        rows = conn.execute(
            sa.text(
                "SELECT id, action, agent_id, ip_address, created_at, request_body "
                "FROM audit_log "
                "WHERE entry_hmac IS NULL "
                "ORDER BY created_at ASC, id ASC"
            )
        ).fetchall()

        prev_hmac = _GENESIS_HMAC
        for row in rows:
            row_id = str(row[0])
            action = row[1] or ""
            agent_id = str(row[2]) if row[2] else ""
            ip = row[3] or ""
            ts_iso_val = _ts_iso(row[4]) if row[4] else ""

            # Deserialise request_body for canonical JSON
            rb = row[5]
            if isinstance(rb, str):
                try:
                    rb = json.loads(rb)
                except (ValueError, TypeError):
                    rb = None
            details_json = _canonical_json(rb)

            entry_hmac = _compute_hmac(
                key=audit_hmac_key,
                prev_hmac=prev_hmac,
                action=action,
                agent_id=agent_id,
                ip=ip,
                ts_iso=ts_iso_val,
                details_json=details_json,
            )

            conn.execute(
                sa.text(
                    "UPDATE audit_log SET prev_hmac = :prev, entry_hmac = :entry "
                    "WHERE id = :id"
                ),
                {"prev": prev_hmac, "entry": entry_hmac, "id": row_id},
            )
            prev_hmac = entry_hmac

        logger.info(
            "audit_log HMAC backfill complete — %d rows processed.", len(rows)
        )

    # Step 4: Add UNIQUE index on entry_hmac (IF NOT EXISTS, skip nulls on PG).
    existing_indexes = {idx["name"] for idx in insp.get_indexes("audit_log")}
    if "ix_audit_log_entry_hmac" not in existing_indexes:
        # PostgreSQL supports partial unique indexes to exclude NULLs.
        # SQLite and others use a plain unique index (NULLs are never equal,
        # so multiple NULLs are allowed by default).
        dialect = conn.dialect.name
        if dialect == "postgresql":
            op.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_audit_log_entry_hmac "
                "ON audit_log (entry_hmac) WHERE entry_hmac IS NOT NULL"
            )
        else:
            op.create_index(
                "ix_audit_log_entry_hmac", "audit_log", ["entry_hmac"], unique=True
            )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)
    existing_indexes = {idx["name"] for idx in insp.get_indexes("audit_log")}

    if "ix_audit_log_entry_hmac" in existing_indexes:
        op.drop_index("ix_audit_log_entry_hmac", table_name="audit_log")

    if "ix_audit_log_created_at" in existing_indexes:
        op.drop_index("ix_audit_log_created_at", table_name="audit_log")

    existing_cols = {col["name"] for col in insp.get_columns("audit_log")}
    if "entry_hmac" in existing_cols:
        op.drop_column("audit_log", "entry_hmac")

    if "prev_hmac" in existing_cols:
        op.drop_column("audit_log", "prev_hmac")
