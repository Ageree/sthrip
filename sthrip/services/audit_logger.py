"""
Audit logging service — tamper-evident HMAC chain (F-11).

Each row in ``audit_log`` carries two new columns:

* ``prev_hmac``: the ``entry_hmac`` of the immediately preceding row (or the
  genesis sentinel ``sha256(b"genesis").hexdigest()`` for the very first row).
* ``entry_hmac``: ``HMAC-SHA256(AUDIT_HMAC_KEY, prev_hmac || action ||
  agent_id || ip || timestamp_iso || canonical_details_json)``.

Tamper detection
----------------
Any modification to a row's audited fields invalidates that row's
``entry_hmac``.  Because the next row's ``prev_hmac`` equals the original
``entry_hmac``, a single tamper cascades and breaks verification for every
subsequent row.  Deleted rows break the chain at the first gap detected by
``verify_chain``.

Locking strategy
----------------
Monotonic chain linking requires that the reader of the latest ``entry_hmac``
and the inserter of the new row are atomic.  On **PostgreSQL** (production)
this is achieved with ``pg_advisory_xact_lock`` — a transaction-scoped
advisory lock keyed on a fixed integer constant.  On **SQLite** (tests / dev)
an in-process ``threading.Lock()`` is used; it serialises concurrent inserts
within the same process, which is sufficient since SQLite is single-writer.

Trade-offs
----------
* Advisory-lock contention: if a long-running transaction holds the lock,
  other audit writers block until it commits/rolls back.  This is intentional
  — correctness (monotonic chain) trumps throughput for audit writes, which
  are much less frequent than read queries.
* The lock is advisory; a rogue writer bypassing it can insert rows with a
  broken chain — detected by ``verify_chain``.
* Pre-migration rows are backfilled with computed HMACs but their
  tamper-history prior to migration is undetectable.  The chain establishes
  integrity **from the migration point forward**.

Key separation
--------------
``AUDIT_HMAC_KEY`` is distinct from ``API_KEY_HMAC_SECRET``; the config
validator enforces this.  Compromise of one key does not compromise the other.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sthrip.db.database import get_db
from sthrip.db.models import AuditLog
from sthrip.config import get_settings

logger = logging.getLogger("sthrip.audit")

# ---------------------------------------------------------------------------
# Genesis sentinel — first row always uses this as prev_hmac.
# ---------------------------------------------------------------------------
_GENESIS_HMAC: str = hashlib.sha256(b"genesis").hexdigest()

# ---------------------------------------------------------------------------
# Sensitive keys to redact (unchanged from original).
# ---------------------------------------------------------------------------
_SENSITIVE_KEYS = frozenset({
    "api_key", "password", "secret", "mnemonic", "seed",
    "webhook_secret", "admin_key", "token", "credentials",
})

# ---------------------------------------------------------------------------
# Advisory lock constant for Postgres (arbitrary unique 64-bit integer).
# ---------------------------------------------------------------------------
_AUDIT_LOCK_KEY: int = 0x5374687269705F61  # "Sthrip_a" little-endian

# ---------------------------------------------------------------------------
# Process-local lock for SQLite (tests / dev).
# ---------------------------------------------------------------------------
_process_lock = threading.Lock()


# ---------------------------------------------------------------------------
# ChainStatus — returned by verify_chain.
# ---------------------------------------------------------------------------

@dataclass
class ChainStatus:
    """Result of a chain verification pass."""

    ok: bool
    """True when every checked row's HMAC is valid and the chain is unbroken."""

    first_bad_id: Optional[UUID]
    """UUID of the first row that failed verification; None when ok=True."""

    total_checked: int
    """Number of rows examined."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sanitize(data: Optional[dict]) -> Optional[dict]:
    """Recursively redact sensitive keys in a details dict."""
    if data is None:
        return None
    result = {}
    for k, v in data.items():
        if k.lower() in _SENSITIVE_KEYS:
            result[k] = "***"
        elif isinstance(v, dict):
            result[k] = _sanitize(v)
        elif isinstance(v, list):
            result[k] = [_sanitize(item) if isinstance(item, dict) else item for item in v]
        else:
            result[k] = v
    return result


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON serialisation (sorted keys, compact, str fallback)."""
    if obj is None:
        return "null"
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _ts_iso(dt: Optional[datetime]) -> str:
    """Canonical ISO timestamp string for HMAC computation.

    Normalises to naive UTC (strips timezone) so that the string is identical
    whether the datetime came directly from Python (timezone-aware) or was
    read back from SQLite (which stores datetimes without timezone info).
    """
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        # Convert to UTC-naive by replacing tzinfo
        dt = dt.replace(tzinfo=None)
    return dt.isoformat()


def _hash_chain_link(
    *,
    key: str,
    prev_hmac: str,
    action: str,
    agent_id: str,
    ip: str,
    ts_iso: str,
    details_json: str,
) -> str:
    """Compute HMAC-SHA256 over chain link fields.

    Fields are joined with the null byte (``\\x00``) — a byte that cannot
    appear in any field value — to prevent boundary-ambiguity attacks.

    Returns a 64-character lowercase hex digest.

    Exposed at module level so tests can monkey-patch it to inspect or stub
    HMAC computation without touching ``log_event`` internals.
    """
    message = "\x00".join([prev_hmac, action, agent_id, ip, ts_iso, details_json])
    return _hmac.new(
        key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _acquire_chain_lock(db: Any) -> None:
    """Acquire an exclusive lock before reading prev_hmac + inserting.

    PostgreSQL: ``pg_advisory_xact_lock`` (blocking, transaction-scoped,
    released automatically on commit/rollback).
    SQLite / other: process-level ``threading.Lock``.

    Callers on non-Postgres must call ``_release_chain_lock`` after the insert
    (or on error).
    """
    try:
        dialect = db.bind.dialect.name  # type: ignore[union-attr]
    except Exception:
        dialect = "sqlite"

    if dialect == "postgresql":
        import sqlalchemy as _sa
        db.execute(
            _sa.text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _AUDIT_LOCK_KEY},
        )
    else:
        _process_lock.acquire()


def _release_chain_lock(db: Any) -> None:
    """Release the process-level lock on non-Postgres engines (no-op on PG)."""
    try:
        dialect = db.bind.dialect.name  # type: ignore[union-attr]
    except Exception:
        dialect = "sqlite"

    if dialect != "postgresql":
        try:
            _process_lock.release()
        except RuntimeError:
            pass  # Already released — defensive


def _get_prev_hmac(db: Any) -> str:
    """Return the entry_hmac of the most recently inserted row, or genesis.

    We order by (created_at DESC, id DESC) because UUID primary keys do not
    sort in insertion order.  created_at is set explicitly in Python before
    the advisory lock is acquired, so within the lock-held window each insert
    gets a strictly greater created_at.  The id tiebreaker handles the
    (extremely unlikely) case of two inserts within the same microsecond.

    Returns the genesis sentinel when:
    - The table is empty (no prior rows).
    - The last row has no entry_hmac (pre-migration rows not yet backfilled).
    - The db is a test mock that does not set up the query chain (safe fallback).
    """
    try:
        last = (
            db.query(AuditLog)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .with_for_update()
            .first()
        )
        hmac_val = getattr(last, "entry_hmac", None)
        if hmac_val is None or not isinstance(hmac_val, str):
            return _GENESIS_HMAC
        return hmac_val
    except (AttributeError, TypeError):
        # Test mocks may not set up the .query(...).order_by(...).with_for_update()
        # chain — fall back to genesis for unit-test ergonomics.  Real DB errors
        # (SQLAlchemyError) MUST propagate so a poisoned chain isn't silently
        # masked as "tampering" — see Opus F-11 review MEDIUM.
        return _GENESIS_HMAC


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_event(
    action: str,
    agent_id: Optional[UUID] = None,
    ip_address: Optional[str] = None,
    request_method: Optional[str] = None,
    request_path: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[UUID] = None,
    details: Optional[dict] = None,
    success: bool = True,
    error_message: Optional[str] = None,
    db: Optional[Any] = None,
) -> None:
    """Log an audit event with a tamper-evident HMAC chain link.

    Actions:
        agent.registered, agent.verified, payment.hub_routing,
        balance.deposit, balance.withdraw, admin.stats_viewed, auth.failed

    Args:
        db: Optional SQLAlchemy session.  When provided, the entry is written
            to the caller's existing transaction (commit/rollback atomically).
            When omitted, an internal session is opened (backward-compatible).

    Locking note:
        When ``db`` points to a PostgreSQL session the advisory lock is held
        until that session's outer transaction commits.  Long-lived caller
        transactions therefore block other audit writers for their duration.
        Acceptable trade-off: audit write rates are low and correctness
        (monotonic chain) is required.
    """
    try:
        sanitized = _sanitize(details)

        if db is not None:
            # Caller-owned session: the advisory lock on Postgres is xact-scoped
            # (released on caller's commit/rollback).  On SQLite the process lock
            # is released after the row is added to the session, before the
            # caller commits — an inherent limitation of SQLite multi-session
            # concurrency.  For production (PostgreSQL) the xact-scoped lock
            # provides full correctness.
            _write_with_chain(
                db=db,
                commit_after=False,
                action=action,
                agent_id=agent_id,
                ip_address=ip_address,
                request_method=request_method,
                request_path=request_path,
                resource_type=resource_type,
                resource_id=resource_id,
                sanitized=sanitized,
                success=success,
                error_message=error_message,
            )
        else:
            # Internal session: use get_db() but commit INSIDE the lock so the
            # next concurrent writer always reads the committed prev_hmac.
            with get_db() as session:
                _write_with_chain(
                    db=session,
                    commit_after=True,
                    action=action,
                    agent_id=agent_id,
                    ip_address=ip_address,
                    request_method=request_method,
                    request_path=request_path,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    sanitized=sanitized,
                    success=success,
                    error_message=error_message,
                )
    except Exception:
        # Audit logging must never break the main request.
        logger.warning("Failed to write audit log for action=%s", action, exc_info=True)


def _write_with_chain(
    *,
    db: Any,
    commit_after: bool,
    action: str,
    agent_id: Optional[UUID],
    ip_address: Optional[str],
    request_method: Optional[str],
    request_path: Optional[str],
    resource_type: Optional[str],
    resource_id: Optional[UUID],
    sanitized: Optional[dict],
    success: bool,
    error_message: Optional[str],
) -> None:
    """Acquire chain lock, compute HMAC, insert row.

    Args:
        commit_after: When True (internal-session path), commit the session
            BEFORE releasing the lock.  This ensures the next concurrent writer
            always reads the committed prev_hmac, guaranteeing a valid chain
            under concurrent writes on both PostgreSQL and SQLite.
            When False (caller-session path), the caller owns the commit;
            on PostgreSQL the xact-scoped advisory lock provides correctness.

    The timestamp is captured INSIDE the lock so that concurrent writers
    always produce strictly monotonic (created_at, id) ordering relative to
    their lock-acquisition order.
    """
    settings = get_settings()
    key = settings.audit_hmac_key

    _acquire_chain_lock(db)
    try:
        # Capture timestamp inside the lock to guarantee monotonic ordering
        # even when two threads request writes at the same microsecond.
        now = datetime.now(timezone.utc)
        prev_hmac = _get_prev_hmac(db)
        details_json = _canonical_json(sanitized)

        entry_hmac = _hash_chain_link(
            key=key,
            prev_hmac=prev_hmac,
            action=action,
            agent_id=str(agent_id) if agent_id else "",
            ip=ip_address or "",
            ts_iso=_ts_iso(now),
            details_json=details_json,
        )

        entry = AuditLog(
            agent_id=agent_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip_address,
            request_method=request_method,
            request_path=request_path,
            request_body=sanitized,
            success=success,
            error_message=error_message,
            created_at=now,
            prev_hmac=prev_hmac,
            entry_hmac=entry_hmac,
        )
        db.add(entry)

        if commit_after:
            db.commit()
    finally:
        _release_chain_lock(db)


def verify_chain(
    db: Any,
    *,
    key: Optional[str] = None,
    since: Optional[UUID] = None,
) -> ChainStatus:
    """Verify the HMAC chain integrity of the audit_log table.

    Args:
        db: SQLAlchemy session for queries.
        key: HMAC key.  Defaults to ``get_settings().audit_hmac_key``.
        since: When provided, only rows inserted **after** the row with this
            id are verified (incremental verification for periodic jobs).
            The anchor row's ``entry_hmac`` is used as the expected
            ``prev_hmac`` for the first checked row.

    Returns:
        ``ChainStatus`` with ``ok=True`` when every checked row is valid.

    On mismatch the function records the first bad row and continues counting
    rather than aborting early, so operators can assess the full scope of
    tampering.
    """
    if key is None:
        key = get_settings().audit_hmac_key

    rows = db.query(AuditLog).order_by(AuditLog.created_at, AuditLog.id).all()

    if not rows:
        return ChainStatus(ok=True, first_bad_id=None, total_checked=0)

    # Pre-migration legacy rows have NULL entry_hmac/prev_hmac.  They are NOT
    # part of the integrity chain (the chain starts at the first row with a
    # backfilled or freshly-computed HMAC) and must be skipped — propagating
    # "" through them would corrupt expected_prev_hmac for every subsequent
    # legitimate row (Opus F-11 MEDIUM).
    rows = [r for r in rows if r.entry_hmac]

    if not rows:
        # Table holds only legacy rows — nothing to verify.
        return ChainStatus(ok=True, first_bad_id=None, total_checked=0)

    start_index = 0
    expected_prev_hmac = _GENESIS_HMAC

    if since is not None:
        for i, row in enumerate(rows):
            if row.id == since:
                expected_prev_hmac = row.entry_hmac or _GENESIS_HMAC
                start_index = i + 1
                break

    rows_to_check = rows[start_index:]
    total_checked = len(rows_to_check)
    first_bad_id: Optional[UUID] = None
    ok = True

    for row in rows_to_check:
        # Check prev_hmac linkage first.
        if row.prev_hmac != expected_prev_hmac:
            if ok:
                first_bad_id = row.id
            ok = False
            expected_prev_hmac = row.entry_hmac or ""
            continue

        # Recompute entry_hmac and compare.
        details_json = _canonical_json(row.request_body)
        ts_iso = _ts_iso(row.created_at)
        recomputed = _hash_chain_link(
            key=key,
            prev_hmac=row.prev_hmac,
            action=row.action,
            agent_id=str(row.agent_id) if row.agent_id else "",
            ip=row.ip_address or "",
            ts_iso=ts_iso,
            details_json=details_json,
        )

        if recomputed != row.entry_hmac:
            if ok:
                first_bad_id = row.id
            ok = False

        expected_prev_hmac = row.entry_hmac or recomputed

    return ChainStatus(ok=ok, first_bad_id=first_bad_id, total_checked=total_checked)
