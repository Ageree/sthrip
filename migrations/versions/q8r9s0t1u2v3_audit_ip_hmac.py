"""Sprint 1 (anonymity-hardening): Audit-log IP scrubbing — replace
``audit_log.ip_address`` (raw VARCHAR(45)) with HMAC under a rotating salt.

Revision ID: q8r9s0t1u2v3
Revises: p7q8r9s0t1u2
Create Date: 2026-05-06

Migration steps
---------------
1. Create ``ip_salts(salt_id PK, secret BYTEA, created_at, retired_at NULL)``.
   IF NOT EXISTS for idempotency.
2. Insert one BOOTSTRAP salt with a deterministic UUID
   ``00000000-0000-0000-0000-000000000001`` and a freshly-generated secret.
   Re-running the migration is a no-op on the bootstrap row (ON CONFLICT DO NOTHING).
3. Add ``audit_log.ip_hmac BYTEA NULL`` and ``audit_log.ip_salt_id UUID NULL``
   (FK to ip_salts.salt_id).
4. Acquire advisory lock 0x5374687269705F49 ("Sthrip_I") on Postgres so a
   concurrent writer cannot race the backfill.
5. Backfill: for each ``audit_log`` row with non-null ``ip_address`` AND
   non-null ``entry_hmac`` → compute ``ip_hmac = HMAC(bootstrap_secret, ip)``
   and recompute ``entry_hmac`` using ``ip_hmac.hex()`` in the canonical
   message slot the raw IP used to occupy.  Same algorithm as the
   ``audit_logger`` runtime, duplicated here to avoid a circular import
   inside alembic env.
6. Drop ``audit_log.ip_address`` (IF EXISTS).
7. Mark the bootstrap salt as retired so future writes pick a fresh one
   (which the runtime ``current_ip_salt`` will lazily create).
8. Release advisory lock.
9. Run a chain-integrity smoke check at the end and raise (aborting deploy)
   if any row's ``prev_hmac`` no longer matches the previous row's
   ``entry_hmac`` — preventing silent forensics degradation in production.

Chain re-backfill (HIGH-1 fix, iter-2)
--------------------------------------
Each row's ``entry_hmac`` is recomputed using ``ip_hmac.hex()`` in the slot
that previously held the raw IP, so its value changes.  Because every row's
``prev_hmac`` originally referenced the prior row's *old* ``entry_hmac``, a
naive backfill that updates only the current row would orphan the chain.
This migration iterates rows in ``(created_at, id)`` order, threads a
``running_prev`` counter through the loop, and updates BOTH ``prev_hmac``
and ``entry_hmac`` so the chain stays linked end-to-end.  Mirrors the
F-11 backfill pattern in ``o6p7q8r9s0t1_audit_hmac_chain.py``.

Forward secrecy
---------------
After this migration the raw client IPs are no longer recoverable from the
audit_log table.  The bootstrap salt's secret is preserved in the ip_salts
row for chain re-verification, but per AD-1 the operator may rotate /
destroy it once the post-migration verifier confirms the chain.

Idempotency
-----------
Column additions, table creation, and FK addition are all guarded by
``IF NOT EXISTS`` / inspect-based checks.  The backfill itself only acts on
rows where ``ip_hmac IS NULL AND ip_address IS NOT NULL``, so re-running
after a partial completion picks up where it left off.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import os
import uuid as _uuid
from datetime import datetime
from typing import Optional

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger("alembic.env")

# Revision identifiers
revision = "q8r9s0t1u2v3"
down_revision = "p7q8r9s0t1u2"
branch_labels = None
depends_on = None

# Bootstrap salt UUID — deterministic so re-running the migration is a no-op.
_BOOTSTRAP_SALT_ID = _uuid.UUID("00000000-0000-0000-0000-000000000001")

_GENESIS_HMAC: str = hashlib.sha256(b"genesis").hexdigest()
_IP_SALT_LOCK_KEY: int = 0x5374687269705F49


def _canonical_json(obj) -> str:
    if obj is None:
        return "null"
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _ts_iso(dt) -> str:
    """Canonical ISO ts string for chain hashing.

    Production (Postgres) hands us a ``datetime`` straight from the driver.
    The SQLite-backed unit-test path receives an ISO-format ``str`` instead;
    we coerce it to ``datetime`` here so canonicalisation matches what the
    runtime ``verify_chain`` (which reads via the ORM) will compute.
    """
    if dt is None:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace(" ", "T"))
        except ValueError:
            return dt
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.isoformat()


def _hash_chain_link(*, key: str, prev_hmac: str, action: str, agent_id: str,
                     ip: str, ts_iso: str, details_json: str) -> str:
    msg = "\x00".join([prev_hmac, action, agent_id, ip, ts_iso, details_json])
    return _hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _compute_ip_hmac(ip: str, salt: bytes) -> bytes:
    return _hmac.new(salt, ip.encode("utf-8"), hashlib.sha256).digest()


def _uuid_type(dialect_name: str):
    """Return the dialect-appropriate UUID column type."""
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import UUID as PG_UUID
        return PG_UUID(as_uuid=True)
    return sa.String(36)


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)
    dialect = conn.dialect.name

    # --- Step 1: create ip_salts -----------------------------------------
    if "ip_salts" not in insp.get_table_names():
        op.create_table(
            "ip_salts",
            sa.Column("salt_id", _uuid_type(dialect), primary_key=True),
            sa.Column("secret", sa.LargeBinary(32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_ip_salts_retired_at", "ip_salts", ["retired_at"])

    # --- Step 2: bootstrap salt ------------------------------------------
    bootstrap_secret = os.urandom(32)
    bootstrap_id_str = str(_BOOTSTRAP_SALT_ID)

    existing = conn.execute(
        sa.text("SELECT 1 FROM ip_salts WHERE salt_id = :id"),
        {"id": bootstrap_id_str},
    ).first()
    if existing is None:
        # Use parameter binding to handle BYTEA/BLOB across dialects.
        conn.execute(
            sa.text(
                "INSERT INTO ip_salts (salt_id, secret, created_at, retired_at) "
                "VALUES (:id, :secret, :created, NULL)"
            ),
            {"id": bootstrap_id_str, "secret": bootstrap_secret,
             "created": datetime.utcnow()},
        )
    else:
        # Already inserted by a previous (failed) run — read the secret back
        # so backfill produces the same HMACs as the prior partial run.
        row = conn.execute(
            sa.text("SELECT secret FROM ip_salts WHERE salt_id = :id"),
            {"id": bootstrap_id_str},
        ).first()
        bootstrap_secret = bytes(row[0])

    # --- Step 3: new audit_log columns -----------------------------------
    audit_cols = {col["name"] for col in insp.get_columns("audit_log")}
    if "ip_hmac" not in audit_cols:
        op.add_column("audit_log", sa.Column("ip_hmac", sa.LargeBinary(32), nullable=True))
    if "ip_salt_id" not in audit_cols:
        op.add_column("audit_log", sa.Column("ip_salt_id", _uuid_type(dialect), nullable=True))
        # FK only on Postgres (SQLite doesn't enforce in-place ALTER FK reliably).
        if dialect == "postgresql":
            op.create_foreign_key(
                "fk_audit_log_ip_salt_id",
                "audit_log", "ip_salts",
                ["ip_salt_id"], ["salt_id"],
            )

    # --- Step 4: advisory lock (Postgres only) ---------------------------
    if dialect == "postgresql":
        conn.execute(
            sa.text("SELECT pg_advisory_lock(:key)"), {"key": _IP_SALT_LOCK_KEY},
        )

    try:
        # --- Step 5: backfill --------------------------------------------
        if "ip_address" in audit_cols:
            audit_hmac_key = os.environ.get("AUDIT_HMAC_KEY", "")
            _backfill_ip_hmac_and_rechain(
                conn=conn,
                bootstrap_secret=bootstrap_secret,
                bootstrap_salt_id=bootstrap_id_str,
                audit_hmac_key=audit_hmac_key,
            )

            # --- Step 6: drop ip_address column --------------------------
            # Use batch_alter_table so SQLite handles the drop too.
            with op.batch_alter_table("audit_log") as batch_op:
                batch_op.drop_column("ip_address")

        # --- Step 7: retire the bootstrap salt ---------------------------
        # Future writes call ``current_ip_salt`` which will mint a fresh row.
        conn.execute(
            sa.text(
                "UPDATE ip_salts SET retired_at = :now "
                "WHERE salt_id = :id AND retired_at IS NULL"
            ),
            {"now": datetime.utcnow(), "id": bootstrap_id_str},
        )

        # --- Step 9: chain integrity smoke check -------------------------
        # Hard-abort the migration (and therefore the deploy) if the
        # backfill produced an inconsistent chain.  This catches HIGH-1
        # style regressions in CI/staging instead of silently degrading
        # forensics in production.
        _assert_chain_linked(conn)

    finally:
        # --- Step 8: release advisory lock -------------------------------
        if dialect == "postgresql":
            conn.execute(
                sa.text("SELECT pg_advisory_unlock(:key)"),
                {"key": _IP_SALT_LOCK_KEY},
            )


def _backfill_ip_hmac_and_rechain(
    *,
    conn,
    bootstrap_secret: bytes,
    bootstrap_salt_id: str,
    audit_hmac_key: str,
) -> int:
    """Recompute ip_hmac for every row carrying a raw ip_address and re-link
    the F-11 chain in (created_at, id) order.

    Each row's new ``entry_hmac`` is derived from the *running* previous
    entry_hmac counter (NOT from the row's stored prev_hmac, which still
    points at the obsolete pre-migration entry_hmac of the prior row).
    Both ``prev_hmac`` and ``entry_hmac`` columns are updated atomically per
    row so the chain stays linked end-to-end.

    Pre-F-11 legacy rows (``entry_hmac IS NULL``) are intentionally skipped:
    they remain outside the integrity chain just as in
    ``o6p7q8r9s0t1_audit_hmac_chain.py``.  ``running_prev`` is NOT advanced
    across them — the chain resumes from the next row that already has an
    entry_hmac set.

    Returns the number of rows updated.
    """
    pending = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE ip_address IS NOT NULL AND ip_hmac IS NULL"
        )
    ).scalar() or 0
    if pending and not audit_hmac_key:
        raise RuntimeError(
            f"AUDIT_HMAC_KEY must be set to backfill {pending} audit_log "
            "rows that carry raw IPs.  Set the env var and re-run."
        )

    rows = conn.execute(
        sa.text(
            "SELECT id, action, agent_id, ip_address, created_at, "
            "request_body, prev_hmac, entry_hmac "
            "FROM audit_log "
            "WHERE ip_address IS NOT NULL AND ip_hmac IS NULL "
            "ORDER BY created_at ASC, id ASC"
        )
    ).fetchall()

    running_prev = _GENESIS_HMAC
    updated = 0
    for row in rows:
        row_id = row[0]
        action = row[1] or ""
        agent_id_str = str(row[2]) if row[2] else ""
        ip = row[3] or ""
        ts_iso_val = _ts_iso(row[4]) if row[4] else ""
        rb = row[5]
        old_entry_hmac = row[7]

        if isinstance(rb, str):
            try:
                rb = json.loads(rb)
            except (ValueError, TypeError):
                rb = None
        details_json = _canonical_json(rb)

        ip_hmac = _compute_ip_hmac(ip, bootstrap_secret)

        if old_entry_hmac is not None and audit_hmac_key:
            new_entry_hmac = _hash_chain_link(
                key=audit_hmac_key,
                prev_hmac=running_prev,
                action=action,
                agent_id=agent_id_str,
                ip=ip_hmac.hex(),
                ts_iso=ts_iso_val,
                details_json=details_json,
            )
            new_prev_hmac = running_prev
            running_prev = new_entry_hmac
        else:
            # Legacy pre-F-11 row — leave its hmac fields untouched and
            # do NOT advance the running counter.
            new_entry_hmac = old_entry_hmac
            new_prev_hmac = row[6]

        conn.execute(
            sa.text(
                "UPDATE audit_log SET ip_hmac = :ip_hmac, "
                "ip_salt_id = :salt_id, prev_hmac = :prev_hmac, "
                "entry_hmac = :entry_hmac WHERE id = :id"
            ),
            {
                "ip_hmac": ip_hmac,
                "salt_id": bootstrap_salt_id,
                "prev_hmac": new_prev_hmac,
                "entry_hmac": new_entry_hmac,
                "id": row_id,
            },
        )
        updated += 1

    logger.info(
        "audit_log ip_hmac backfill complete — %d rows rewritten under "
        "bootstrap salt", updated,
    )
    return updated


def _assert_chain_linked(conn) -> None:
    """Raise RuntimeError if the post-backfill chain is broken.

    Iterates rows in ``(created_at, id)`` order and asserts that for every
    non-legacy row, ``prev_hmac`` equals the previous non-legacy row's
    ``entry_hmac``, with the genesis sentinel anchoring row 0.

    Legacy rows (``entry_hmac IS NULL``) are skipped — they are outside
    the chain by design.
    """
    rows = conn.execute(
        sa.text(
            "SELECT id, prev_hmac, entry_hmac FROM audit_log "
            "WHERE entry_hmac IS NOT NULL "
            "ORDER BY created_at ASC, id ASC"
        )
    ).fetchall()

    expected_prev = _GENESIS_HMAC
    for row in rows:
        row_prev = row[1] or ""
        if row_prev != expected_prev:
            raise RuntimeError(
                f"audit_log chain integrity broken at row id={row[0]!r}: "
                f"prev_hmac={row_prev!r} expected={expected_prev!r}"
            )
        expected_prev = row[2]


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)
    dialect = conn.dialect.name

    audit_cols = {col["name"] for col in insp.get_columns("audit_log")}

    # Restore ip_address column (data is destroyed by design — the column is
    # restored as NULL so existing code that references it can compile).
    if "ip_address" not in audit_cols:
        with op.batch_alter_table("audit_log") as batch_op:
            batch_op.add_column(sa.Column("ip_address", sa.String(45), nullable=True))

    # Drop new columns.
    if "ip_salt_id" in audit_cols:
        if dialect == "postgresql":
            try:
                op.drop_constraint("fk_audit_log_ip_salt_id", "audit_log", type_="foreignkey")
            except Exception:
                pass
        with op.batch_alter_table("audit_log") as batch_op:
            batch_op.drop_column("ip_salt_id")
    if "ip_hmac" in audit_cols:
        with op.batch_alter_table("audit_log") as batch_op:
            batch_op.drop_column("ip_hmac")

    # Drop ip_salts table.
    if "ip_salts" in insp.get_table_names():
        existing_indexes = {idx["name"] for idx in insp.get_indexes("ip_salts")}
        if "ix_ip_salts_retired_at" in existing_indexes:
            op.drop_index("ix_ip_salts_retired_at", table_name="ip_salts")
        op.drop_table("ip_salts")
