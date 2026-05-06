"""
Sprint 1 (anonymity-hardening): IP-salt service.

Implements AD-1: keyed-HMAC over raw client IPs with a salt that is rotated on
a configurable cadence.  Old salts are destroyed once they age past a hard
threshold so that a leaked snapshot cannot be replayed against the IPv4 space.

Key invariants
--------------
- ``current_ip_salt(db)`` always returns ONE active row (``retired_at IS NULL``).
  If none exists yet, a fresh 32-byte secret is created and committed.
- ``rotate_ip_salt(db)`` retires the currently-active salt and creates a new
  active one.  Salts older than ``2 * IP_SALT_ROTATION_DAYS`` are zero-ed
  out (or deleted) so the underlying secret is no longer recoverable.
- ``compute_ip_hmac(ip, salt)`` is a pure function over UTF-8 IP bytes; it
  performs no I/O.  Callers in the audit logger pass the result as a 32-byte
  ``bytes`` value, optionally hex-encoding when feeding into the existing
  text-only HMAC chain link function.

Configuration
-------------
``IP_SALT_ROTATION_DAYS`` env var (default 7, accepts 1..30) — how often the
operator intends to rotate.  The service does NOT enforce a schedule (that is
a Railway cron concern); it does enforce the destruction threshold whenever
``rotate_ip_salt`` is called.

Threading / locking
-------------------
A short ``pg_advisory_xact_lock`` (Postgres) or process-level
``threading.Lock`` (SQLite) is held while reading the active salt so that
two concurrent writers do not race to bootstrap conflicting active rows.
The lock is released as soon as the row is read (or freshly inserted).
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple
from uuid import UUID, uuid4

import sqlalchemy as sa

from sthrip.db.models import IpSalt

logger = logging.getLogger("sthrip.ip_salt")

# Postgres advisory lock key for salt mutations.  Distinct from the audit
# chain lock so operations on each table do not block each other.
_IP_SALT_LOCK_KEY: int = 0x5374687269705F49  # "Sthrip_I"

# Process-level fallback for SQLite.
_process_lock = threading.Lock()

_DEFAULT_ROTATION_DAYS: int = 7
_MIN_ROTATION_DAYS: int = 1
_MAX_ROTATION_DAYS: int = 30

# Cached parsed env value — avoids re-parsing on every call but allows tests to
# override via ``_reset_config_cache`` after monkeypatching.
_cached_rotation_days: Optional[int] = None


def _reset_config_cache() -> None:
    """Test hook — clears the cached rotation-days value."""
    global _cached_rotation_days
    _cached_rotation_days = None


def get_rotation_days() -> int:
    """Return the configured rotation cadence (days), clamped to [1, 30]."""
    global _cached_rotation_days
    if _cached_rotation_days is not None:
        return _cached_rotation_days
    raw = os.environ.get("IP_SALT_ROTATION_DAYS", "").strip()
    if not raw:
        days = _DEFAULT_ROTATION_DAYS
    else:
        try:
            days = int(raw)
        except ValueError:
            logger.warning(
                "Invalid IP_SALT_ROTATION_DAYS=%r — falling back to default %d",
                raw, _DEFAULT_ROTATION_DAYS,
            )
            days = _DEFAULT_ROTATION_DAYS
    days = max(_MIN_ROTATION_DAYS, min(_MAX_ROTATION_DAYS, days))
    _cached_rotation_days = days
    return days


def compute_ip_hmac(ip: str, salt_secret: bytes) -> bytes:
    """Return HMAC-SHA256(salt_secret, ip.utf8) as a 32-byte digest.

    Pure function — no I/O.  Empty string IPs are still hashed (the audit
    logger feeds an empty-string IP only when the caller explicitly provided
    no IP, in which case the hash is never persisted).
    """
    if not isinstance(salt_secret, (bytes, bytearray)):
        raise TypeError("salt_secret must be bytes")
    return _hmac.new(bytes(salt_secret), ip.encode("utf-8"), hashlib.sha256).digest()


def _acquire_lock(db: Any) -> bool:
    """Acquire the salt-mutation lock; return True if a process-lock was taken
    (caller is responsible for releasing on the SQLite path)."""
    try:
        dialect = db.bind.dialect.name  # type: ignore[union-attr]
    except Exception:
        dialect = "sqlite"
    if dialect == "postgresql":
        db.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": _IP_SALT_LOCK_KEY})
        return False
    _process_lock.acquire()
    return True


def _release_lock(took: bool) -> None:
    if took:
        try:
            _process_lock.release()
        except RuntimeError:
            pass


def _utc_now() -> datetime:
    """SQLite drops timezone info on store/retrieve, so we use naive-UTC
    consistently to keep round-trip comparisons simple."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def current_ip_salt(db: Any) -> Tuple[UUID, bytes]:
    """Return ``(salt_id, secret)`` for the active salt, creating one if needed.

    Reads/inserts inside a short advisory lock so concurrent writers do not
    bootstrap competing active rows.  Caller is expected to commit the
    enclosing transaction (or pass an autocommitting session).
    """
    took = _acquire_lock(db)
    try:
        active = (
            db.query(IpSalt)
            .filter(IpSalt.retired_at.is_(None))
            .order_by(IpSalt.created_at.desc())
            .first()
        )
        if active is not None:
            return active.salt_id, bytes(active.secret)

        # Bootstrap a brand-new active row.
        new_id = uuid4()
        secret = secrets.token_bytes(32)
        row = IpSalt(
            salt_id=new_id,
            secret=secret,
            created_at=_utc_now(),
            retired_at=None,
        )
        db.add(row)
        db.flush()
        return new_id, secret
    finally:
        _release_lock(took)


def rotate_ip_salt(db: Any) -> UUID:
    """Retire the active salt, mint a fresh one, and destroy salts older than
    ``2 * IP_SALT_ROTATION_DAYS``.  Returns the new salt_id."""
    rotation_days = get_rotation_days()
    destroy_after = timedelta(days=2 * rotation_days)
    now = _utc_now()

    took = _acquire_lock(db)
    try:
        # Step 1: retire whatever is currently active.
        actives = db.query(IpSalt).filter(IpSalt.retired_at.is_(None)).all()
        for row in actives:
            row.retired_at = now

        # Step 2: mint a new active salt.
        new_id = uuid4()
        new_row = IpSalt(
            salt_id=new_id,
            secret=secrets.token_bytes(32),
            created_at=now,
            retired_at=None,
        )
        db.add(new_row)

        # Step 3: destroy salts past the hard threshold.
        threshold = now - destroy_after
        ancient = (
            db.query(IpSalt)
            .filter(IpSalt.retired_at.isnot(None))
            .filter(IpSalt.created_at < threshold)
            .all()
        )
        for row in ancient:
            # Preserve row (FK targets in audit_log may still reference the
            # salt_id) but zero the secret so the HMAC cannot be replayed.
            row.secret = b"\x00" * 32

        db.flush()
        return new_id
    finally:
        _release_lock(took)
