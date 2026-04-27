"""
TDD tests for F-11: Tamper-evident HMAC chain on audit_log.

Tests are written FIRST (RED phase) — they will fail until the implementation
is in place.

Chain invariants under test:
- First row: prev_hmac == sha256(b"genesis").hexdigest()
- Nth row: prev_hmac == entry_hmac of (N-1)th row (ordered by id sequence)
- entry_hmac == HMAC-SHA256(key, prev_hmac || action || agent_id || ip || ts_iso || details_json)
- Tamper: flipping any field on any row breaks that row's entry_hmac and all
  subsequent prev_hmac values.
- Delete: removing a row breaks the next row's prev_hmac.
- Concurrent writes: two parallel log_event calls still produce a valid chain
  (advisory lock / process lock ensures monotonic linking).
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import threading
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GENESIS_HMAC: str = hashlib.sha256(b"genesis").hexdigest()
_TEST_AUDIT_KEY = "a" * 32  # 32-char key, valid for dev + non-dev environments


def _make_engine():
    """SQLite in-memory engine with AuditLog table only."""
    from sthrip.db.models import Base, AuditLog  # noqa: F401 — ensures table created

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Only create the audit_log table (and Agent, because AuditLog has FK to agents)
    from sthrip.db.models import Agent

    Base.metadata.create_all(engine, tables=[Agent.__table__, AuditLog.__table__])
    return engine


def _session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def _get_all_rows(session):
    """Return all AuditLog rows ordered by (created_at, id) — insertion order."""
    from sthrip.db.models import AuditLog

    return session.query(AuditLog).order_by(AuditLog.created_at, AuditLog.id).all()


# ---------------------------------------------------------------------------
# Test: AuditLog model has the two new columns
# ---------------------------------------------------------------------------


def test_auditlog_has_prev_hmac_column():
    """AuditLog model must expose prev_hmac attribute after migration."""
    from sthrip.db.models import AuditLog

    cols = {c.name for c in AuditLog.__table__.columns}
    assert "prev_hmac" in cols, "AuditLog is missing prev_hmac column"


def test_auditlog_has_entry_hmac_column():
    """AuditLog model must expose entry_hmac attribute after migration."""
    from sthrip.db.models import AuditLog

    cols = {c.name for c in AuditLog.__table__.columns}
    assert "entry_hmac" in cols, "AuditLog is missing entry_hmac column"


# ---------------------------------------------------------------------------
# Test: _hash_chain_link produces deterministic HMAC
# ---------------------------------------------------------------------------


def test_hash_chain_link_deterministic():
    """_hash_chain_link returns same value for same inputs (no randomness)."""
    from sthrip.services.audit_logger import _hash_chain_link

    result1 = _hash_chain_link(
        key=_TEST_AUDIT_KEY,
        prev_hmac=_GENESIS_HMAC,
        action="test.action",
        agent_id="abc-123",
        ip="127.0.0.1",
        ts_iso="2026-01-01T00:00:00+00:00",
        details_json='{"foo":"bar"}',
    )
    result2 = _hash_chain_link(
        key=_TEST_AUDIT_KEY,
        prev_hmac=_GENESIS_HMAC,
        action="test.action",
        agent_id="abc-123",
        ip="127.0.0.1",
        ts_iso="2026-01-01T00:00:00+00:00",
        details_json='{"foo":"bar"}',
    )
    assert result1 == result2
    assert len(result1) == 64  # hex-encoded SHA-256


def test_hash_chain_link_changes_with_different_key():
    """Different key produces different HMAC."""
    from sthrip.services.audit_logger import _hash_chain_link

    h1 = _hash_chain_link(
        key=_TEST_AUDIT_KEY,
        prev_hmac=_GENESIS_HMAC,
        action="a",
        agent_id="",
        ip="",
        ts_iso="",
        details_json="null",
    )
    h2 = _hash_chain_link(
        key="b" * 32,
        prev_hmac=_GENESIS_HMAC,
        action="a",
        agent_id="",
        ip="",
        ts_iso="",
        details_json="null",
    )
    assert h1 != h2


# ---------------------------------------------------------------------------
# Test: Forward chain — each row links to its predecessor
# ---------------------------------------------------------------------------


def test_single_row_genesis_prev_hmac():
    """Single log_event call sets prev_hmac to sha256(b'genesis').hexdigest()."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event

        session = SessionLocal()
        log_event("test.action", ip_address="1.2.3.4", db=session)
        session.commit()

        rows = _get_all_rows(session)
        assert len(rows) == 1
        assert rows[0].prev_hmac == _GENESIS_HMAC
        assert rows[0].entry_hmac is not None
        assert len(rows[0].entry_hmac) == 64
        session.close()


def test_forward_chain_two_rows():
    """Second row's prev_hmac equals first row's entry_hmac."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event

        session = SessionLocal()
        log_event("action.first", db=session)
        session.commit()
        log_event("action.second", db=session)
        session.commit()

        rows = _get_all_rows(session)
        assert len(rows) == 2
        assert rows[0].prev_hmac == _GENESIS_HMAC
        assert rows[1].prev_hmac == rows[0].entry_hmac
        session.close()


def test_forward_chain_five_rows():
    """Chain links correctly across five sequential log_event calls."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event

        session = SessionLocal()
        for i in range(5):
            log_event(f"action.{i}", db=session)
            session.commit()

        rows = _get_all_rows(session)
        assert len(rows) == 5
        assert rows[0].prev_hmac == _GENESIS_HMAC
        for i in range(1, 5):
            assert rows[i].prev_hmac == rows[i - 1].entry_hmac, (
                f"Chain broken at row {i}: prev_hmac={rows[i].prev_hmac!r} "
                f"!= entry_hmac={rows[i - 1].entry_hmac!r}"
            )
        session.close()


# ---------------------------------------------------------------------------
# Test: entry_hmac matches recomputed value
# ---------------------------------------------------------------------------


def test_entry_hmac_matches_recomputed():
    """entry_hmac stored on each row matches HMAC recomputed from its fields."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event, _hash_chain_link

        session = SessionLocal()
        log_event("verify.action", ip_address="10.0.0.1", db=session)
        session.commit()

        row = _get_all_rows(session)[0]
        details_json = json.dumps(row.request_body, sort_keys=True, separators=(",", ":"), default=str) \
            if row.request_body is not None else "null"
        # Use _ts_iso for consistent timezone-stripped timestamp matching
        from sthrip.services.audit_logger import _ts_iso
        recomputed = _hash_chain_link(
            key=_TEST_AUDIT_KEY,
            prev_hmac=row.prev_hmac,
            action=row.action,
            agent_id=str(row.agent_id) if row.agent_id else "",
            ip=row.ip_address or "",
            ts_iso=_ts_iso(row.created_at),
            details_json=details_json,
        )
        assert row.entry_hmac == recomputed
        session.close()


# ---------------------------------------------------------------------------
# Test: Tamper detection
# ---------------------------------------------------------------------------


def test_tamper_detect_action_field():
    """Flipping the action field on a row causes verify_chain to report mismatch."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event

        session = SessionLocal()
        log_event("original.action", db=session)
        session.commit()
        log_event("subsequent.action", db=session)
        session.commit()

        # Tamper: change action on row 0
        from sthrip.db.models import AuditLog

        row = session.query(AuditLog).order_by(AuditLog.id).first()
        row.action = "tampered.action"  # noqa: direct mutation for test only
        session.commit()

        from sthrip.services.audit_logger import verify_chain

        status = verify_chain(session, key=_TEST_AUDIT_KEY)
        assert not status.ok
        assert status.first_bad_id is not None
        session.close()


def test_tamper_detect_ip_field():
    """Flipping the ip_address field on a row causes verify_chain to report mismatch."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event

        session = SessionLocal()
        log_event("some.action", ip_address="1.2.3.4", db=session)
        session.commit()

        from sthrip.db.models import AuditLog

        row = session.query(AuditLog).order_by(AuditLog.id).first()
        row.ip_address = "9.9.9.9"
        session.commit()

        from sthrip.services.audit_logger import verify_chain

        status = verify_chain(session, key=_TEST_AUDIT_KEY)
        assert not status.ok
        session.close()


def test_tamper_detect_entry_hmac_itself():
    """Replacing entry_hmac with an arbitrary value triggers detection."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event

        session = SessionLocal()
        log_event("real.action", db=session)
        session.commit()
        log_event("next.action", db=session)
        session.commit()

        from sthrip.db.models import AuditLog

        # Replace entry_hmac on row 0 with fake value
        row = session.query(AuditLog).order_by(AuditLog.id).first()
        row.entry_hmac = "a" * 64
        session.commit()

        from sthrip.services.audit_logger import verify_chain

        status = verify_chain(session, key=_TEST_AUDIT_KEY)
        # Row 0 entry_hmac doesn't match computed → bad; row 1 prev_hmac doesn't match row 0 entry_hmac
        assert not status.ok
        session.close()


def test_tamper_cascades_to_subsequent_rows():
    """After tampering row N, all rows N+1..M also fail verification."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event

        session = SessionLocal()
        for i in range(4):
            log_event(f"row.{i}", db=session)
            session.commit()

        from sthrip.db.models import AuditLog

        rows = session.query(AuditLog).order_by(AuditLog.id).all()
        # Tamper row index 1 (middle)
        rows[1].action = "hacked"
        session.commit()

        from sthrip.services.audit_logger import verify_chain

        status = verify_chain(session, key=_TEST_AUDIT_KEY)
        assert not status.ok
        # first_bad_id should be row 1's id (0-indexed row 1)
        assert status.first_bad_id == rows[1].id
        session.close()


# ---------------------------------------------------------------------------
# Test: Delete detection
# ---------------------------------------------------------------------------


def test_delete_row_breaks_chain():
    """Deleting a row in the middle causes the next row's prev_hmac to mismatch."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event

        session = SessionLocal()
        log_event("row.0", db=session)
        session.commit()
        log_event("row.1", db=session)
        session.commit()
        log_event("row.2", db=session)
        session.commit()

        from sthrip.db.models import AuditLog

        rows = session.query(AuditLog).order_by(AuditLog.created_at, AuditLog.id).all()
        # Delete row 1 (middle row)
        session.delete(rows[1])
        session.commit()

        from sthrip.services.audit_logger import verify_chain

        status = verify_chain(session, key=_TEST_AUDIT_KEY)
        assert not status.ok
        session.close()


# ---------------------------------------------------------------------------
# Test: verify_chain returns ok=True on a clean chain
# ---------------------------------------------------------------------------


def test_verify_chain_ok_on_clean_chain():
    """verify_chain returns ChainStatus(ok=True) when no tampering occurred."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event, verify_chain

        session = SessionLocal()
        for i in range(3):
            log_event(f"clean.{i}", db=session)
            session.commit()

        status = verify_chain(session, key=_TEST_AUDIT_KEY)
        assert status.ok
        assert status.first_bad_id is None
        assert status.total_checked == 3
        session.close()


def test_verify_chain_empty_table_ok():
    """verify_chain returns ok=True when there are no rows (nothing to verify)."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    from sthrip.services.audit_logger import verify_chain

    session = SessionLocal()
    status = verify_chain(session, key=_TEST_AUDIT_KEY)
    assert status.ok
    assert status.total_checked == 0
    session.close()


# ---------------------------------------------------------------------------
# Test: Concurrent writes produce a valid chain
# ---------------------------------------------------------------------------


def test_concurrent_writes_produce_valid_chain(tmp_path):
    """Two threads calling log_event concurrently still produce a valid chain.

    Uses the REAL _acquire_chain_lock / _release_chain_lock (process
    threading.Lock) to serialise writes.  log_event is called WITHOUT a db
    parameter (db=None), so each call uses an internal session that commits
    atomically inside the lock, ensuring the next writer always reads the
    committed prev_hmac.

    Each thread writes 5 rows; after both finish the 10 rows must form a
    contiguous valid chain.
    """
    from contextlib import contextmanager
    from sthrip.db.models import Base, AuditLog, Agent
    from sthrip.db.database import get_db as real_get_db

    db_path = str(tmp_path / "concurrent_test.db")
    file_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    from sqlalchemy import event as sa_event

    @sa_event.listens_for(file_engine, "connect")
    def set_wal_mode(dbapi_connection, connection_record):
        dbapi_connection.execute("PRAGMA journal_mode=WAL")

    Base.metadata.create_all(file_engine, tables=[Agent.__table__, AuditLog.__table__])
    FileSession = sessionmaker(bind=file_engine, expire_on_commit=False)

    @contextmanager
    def test_get_db():
        session = FileSession()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    errors: list[Exception] = []

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger.get_db", side_effect=test_get_db):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event, verify_chain

        def _worker(thread_id: int) -> None:
            try:
                for i in range(5):
                    # db=None → uses internal session that commits inside the lock
                    log_event(f"thread.{thread_id}.row.{i}")
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_worker, args=(1,))
        t2 = threading.Thread(target=_worker, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Thread errors: {errors}"

        check_session = FileSession()
        status = verify_chain(check_session, key=_TEST_AUDIT_KEY)
        assert status.ok, (
            f"Chain invalid after concurrent writes: first_bad_id={status.first_bad_id}, "
            f"total_checked={status.total_checked}"
        )
        assert status.total_checked == 10
        check_session.close()
        file_engine.dispose()


# ---------------------------------------------------------------------------
# Test: ChainStatus dataclass shape
# ---------------------------------------------------------------------------


def test_chain_status_fields():
    """ChainStatus must have ok, first_bad_id, and total_checked fields."""
    from sthrip.services.audit_logger import ChainStatus

    s = ChainStatus(ok=True, first_bad_id=None, total_checked=42)
    assert s.ok is True
    assert s.first_bad_id is None
    assert s.total_checked == 42


# ---------------------------------------------------------------------------
# Test: config validation — AUDIT_HMAC_KEY required in non-dev
# ---------------------------------------------------------------------------


def test_config_audit_hmac_key_required_in_production(monkeypatch):
    """Settings raises ValueError when AUDIT_HMAC_KEY is missing in production."""
    import os
    from sthrip.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 64)
    monkeypatch.setenv("API_KEY_HMAC_SECRET", "a" * 64)
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", "uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=")
    monkeypatch.setenv("MONERO_RPC_HOST", "monero-wallet-rpc.railway.internal")
    monkeypatch.setenv("MONERO_RPC_PASS", "a" * 32)
    monkeypatch.setenv("MONERO_NETWORK", "mainnet")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/sthrip")
    monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)

    with pytest.raises((ValueError, SystemExit)):
        get_settings()

    get_settings.cache_clear()


def test_config_audit_hmac_key_too_short_in_production(monkeypatch):
    """Settings raises ValueError when AUDIT_HMAC_KEY is shorter than 32 chars in production."""
    from sthrip.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 64)
    monkeypatch.setenv("API_KEY_HMAC_SECRET", "a" * 64)
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", "uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=")
    monkeypatch.setenv("MONERO_RPC_HOST", "monero-wallet-rpc.railway.internal")
    monkeypatch.setenv("MONERO_RPC_PASS", "a" * 32)
    monkeypatch.setenv("MONERO_NETWORK", "mainnet")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/sthrip")
    monkeypatch.setenv("AUDIT_HMAC_KEY", "tooshort")

    with pytest.raises((ValueError, SystemExit)):
        get_settings()

    get_settings.cache_clear()


def test_config_audit_hmac_key_not_same_as_api_key_hmac_secret(monkeypatch):
    """Settings raises ValueError when AUDIT_HMAC_KEY equals API_KEY_HMAC_SECRET in non-dev."""
    from sthrip.config import get_settings

    get_settings.cache_clear()
    shared = "b" * 64
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 64)
    monkeypatch.setenv("API_KEY_HMAC_SECRET", shared)
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", "uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=")
    monkeypatch.setenv("MONERO_RPC_HOST", "monero-wallet-rpc.railway.internal")
    monkeypatch.setenv("MONERO_RPC_PASS", "a" * 32)
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/sthrip")
    monkeypatch.setenv("AUDIT_HMAC_KEY", shared)

    with pytest.raises((ValueError, SystemExit)):
        get_settings()

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Test: verify_chain since= parameter limits scope
# ---------------------------------------------------------------------------


def test_verify_chain_since_limits_scope():
    """verify_chain(since=<id>) only checks rows with id > since."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event, verify_chain
        from sthrip.db.models import AuditLog

        session = SessionLocal()
        for i in range(4):
            log_event(f"row.{i}", db=session)
            session.commit()

        # Use same ordering as verify_chain (created_at, id)
        rows = session.query(AuditLog).order_by(AuditLog.created_at, AuditLog.id).all()
        # Tamper row 0 — tampering should be invisible when since=rows[1].id
        rows[0].action = "tampered"
        session.commit()

        # since=rows[1].id → only checks rows[2], rows[3] (rows after row 1)
        status = verify_chain(session, key=_TEST_AUDIT_KEY, since=rows[1].id)
        # rows[2] and rows[3] are untouched — but their prev_hmac links back through row[1]
        # which itself is clean; row[0] tamper is outside since window
        assert status.total_checked == 2
        session.close()
