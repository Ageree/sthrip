"""
Sprint 1 (anonymity-hardening): Audit-log IP scrubbing + request_body allowlist.

Tests cover:
- IpSalt model + ip_salt_service rotation/lookup/HMAC computation
- AuditLog has ip_hmac (BYTEA) + ip_salt_id columns; ip_address column gone
- log_event() hashes IPs to ip_hmac before persistence (raw IP never stored)
- _AUDIT_REQUEST_BODY_ALLOWLIST: known action filters keys, unknown → None
- HMAC chain stays valid: _hash_chain_link consumes hex(ip_hmac)
- Migration round-trip: upgrade → downgrade → upgrade
- Backfill preserves chain integrity: pre-existing rows retain valid entry_hmac
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


_TEST_AUDIT_KEY = "a" * 32
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_engine():
    """SQLite in-memory engine with AuditLog + IpSalt + Agent tables."""
    from sthrip.db.models import Base, AuditLog, Agent, IpSalt

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[Agent.__table__, IpSalt.__table__, AuditLog.__table__],
    )
    return engine


def _session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_audit_log_schema_has_ip_hmac_not_ip_address():
    """AC #4: AuditLog has ip_hmac + ip_salt_id columns; ip_address removed."""
    from sthrip.db.models import AuditLog

    cols = {c.name for c in AuditLog.__table__.columns}
    assert "ip_hmac" in cols, "AuditLog is missing ip_hmac column"
    assert "ip_salt_id" in cols, "AuditLog is missing ip_salt_id column"
    assert "ip_address" not in cols, (
        "Legacy ip_address column must be removed (raw IPs are PII per AD-1)"
    )


def test_ip_salts_table_exists():
    """AD-1: ip_salts(salt_id PK, secret, created_at, retired_at) table exists."""
    from sthrip.db.models import IpSalt

    cols = {c.name for c in IpSalt.__table__.columns}
    assert {"salt_id", "secret", "created_at", "retired_at"}.issubset(cols)


# ---------------------------------------------------------------------------
# ip_salt_service tests
# ---------------------------------------------------------------------------


def test_compute_ip_hmac_is_deterministic_with_same_salt():
    """AC #2: same IP + same salt → byte-identical HMAC of length 32."""
    from sthrip.services.ip_salt_service import compute_ip_hmac

    salt = b"\x01" * 32
    h1 = compute_ip_hmac("203.0.113.42", salt)
    h2 = compute_ip_hmac("203.0.113.42", salt)
    assert h1 == h2
    assert isinstance(h1, bytes)
    assert len(h1) == 32  # SHA-256 digest


def test_compute_ip_hmac_differs_for_different_ips():
    """Different IPs under same salt produce different HMACs."""
    from sthrip.services.ip_salt_service import compute_ip_hmac

    salt = b"\x02" * 32
    h_a = compute_ip_hmac("1.1.1.1", salt)
    h_b = compute_ip_hmac("2.2.2.2", salt)
    assert h_a != h_b


def test_rotation_produces_different_hmac_for_same_ip():
    """AC #3: same IP under different salts → different HMACs."""
    from sthrip.services.ip_salt_service import compute_ip_hmac

    salt_a = b"\x03" * 32
    salt_b = b"\x04" * 32
    assert compute_ip_hmac("8.8.8.8", salt_a) != compute_ip_hmac("8.8.8.8", salt_b)


def test_current_ip_salt_creates_bootstrap_when_empty():
    """First call on empty table creates an active salt and returns it."""
    from sthrip.services.ip_salt_service import current_ip_salt
    from sthrip.db.models import IpSalt

    engine = _make_engine()
    SessionLocal = _session_factory(engine)
    session = SessionLocal()

    salt_id, secret = current_ip_salt(session)
    session.commit()

    assert isinstance(salt_id, uuid.UUID)
    assert isinstance(secret, (bytes, bytearray))
    assert len(secret) == 32
    rows = session.query(IpSalt).all()
    assert len(rows) == 1
    assert rows[0].retired_at is None
    session.close()


def test_rotate_ip_salt_retires_old_creates_new():
    """AC #7: after rotation, prior active salt has retired_at set; new one is current."""
    from sthrip.services.ip_salt_service import current_ip_salt, rotate_ip_salt
    from sthrip.db.models import IpSalt

    engine = _make_engine()
    SessionLocal = _session_factory(engine)
    session = SessionLocal()

    old_id, old_secret = current_ip_salt(session)
    session.commit()

    new_id = rotate_ip_salt(session)
    session.commit()

    assert new_id != old_id
    new_id_2, new_secret = current_ip_salt(session)
    session.commit()
    assert new_id_2 == new_id
    assert new_secret != old_secret

    # Exactly one active row.
    active = session.query(IpSalt).filter(IpSalt.retired_at.is_(None)).all()
    assert len(active) == 1
    assert active[0].salt_id == new_id

    # Old row has retired_at set.
    old_row = session.query(IpSalt).filter(IpSalt.salt_id == old_id).first()
    assert old_row.retired_at is not None
    session.close()


def test_rotate_destroys_salts_beyond_double_window(monkeypatch):
    """AC #8: rotate_ip_salt deletes (or zeroes secret of) salts older than 2 * IP_SALT_ROTATION_DAYS."""
    monkeypatch.setenv("IP_SALT_ROTATION_DAYS", "7")
    from sthrip.services import ip_salt_service
    # Reload module-level config
    ip_salt_service._reset_config_cache()
    from sthrip.services.ip_salt_service import rotate_ip_salt
    from sthrip.db.models import IpSalt

    engine = _make_engine()
    SessionLocal = _session_factory(engine)
    session = SessionLocal()

    # Insert an "ancient" retired salt 30 days old (beyond 2*7=14d threshold).
    ancient_id = uuid.uuid4()
    ancient = IpSalt(
        salt_id=ancient_id,
        secret=b"\xff" * 32,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30),
        retired_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30),
    )
    session.add(ancient)
    session.commit()

    rotate_ip_salt(session)
    session.commit()

    found = session.query(IpSalt).filter(IpSalt.salt_id == ancient_id).first()
    if found is not None:
        # If kept around (e.g. for FK integrity), the secret MUST be zeroed.
        assert bytes(found.secret) == b"\x00" * 32, (
            "Salt beyond 2x window must be deleted or have its secret zeroed"
        )
    session.close()


# ---------------------------------------------------------------------------
# log_event integration tests
# ---------------------------------------------------------------------------


def test_log_event_writes_hmac_not_raw_ip():
    """AC #4: log_event(ip_address=...) persists ip_hmac, never raw IP string."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event
        from sthrip.db.models import AuditLog

        session = SessionLocal()
        log_event("agent.registered", ip_address="203.0.113.42",
                  details={"agent_name": "alice"}, db=session)
        session.commit()

        rows = session.query(AuditLog).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.ip_hmac is not None
        assert isinstance(row.ip_hmac, (bytes, bytearray))
        assert len(row.ip_hmac) == 32
        # AuditLog model must NOT carry an ip_address attribute anymore.
        assert not hasattr(row, "ip_address") or getattr(row, "ip_address", None) is None
        session.close()


def test_log_event_no_ip_yields_null_hmac():
    """log_event(ip_address=None) leaves ip_hmac NULL and does not crash."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event
        from sthrip.db.models import AuditLog

        session = SessionLocal()
        log_event("agent.registered", ip_address=None,
                  details={"agent_name": "alice"}, db=session)
        session.commit()

        rows = session.query(AuditLog).all()
        assert rows[0].ip_hmac is None
        session.close()


# ---------------------------------------------------------------------------
# Allowlist tests
# ---------------------------------------------------------------------------


def test_allowlist_filters_disallowed_keys():
    """AC #5: known action filters request_body to allowlisted keys only."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event
        from sthrip.db.models import AuditLog

        session = SessionLocal()
        log_event(
            "agent.registered",
            details={"agent_name": "alice", "webhook_url": "leak", "amount": "1"},
            db=session,
        )
        session.commit()

        rows = session.query(AuditLog).all()
        # Only "agent_name" (allowlisted for agent.registered) should survive.
        assert rows[0].request_body == {"agent_name": "alice"}
        session.close()


def test_allowlist_unknown_action_defaults_to_none():
    """AC #6: action without an allowlist entry persists request_body=None (no raise)."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event
        from sthrip.db.models import AuditLog

        session = SessionLocal()
        log_event(
            "totally.unmapped.action",
            details={"foo": "bar", "secret": "xxx"},
            db=session,
        )
        session.commit()

        rows = session.query(AuditLog).all()
        assert rows[0].request_body is None
        session.close()


def test_allowlist_empty_details_is_safe():
    """log_event(details=None) on a known action persists request_body=None."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event
        from sthrip.db.models import AuditLog

        session = SessionLocal()
        log_event("agent.registered", details=None, db=session)
        session.commit()

        rows = session.query(AuditLog).all()
        assert rows[0].request_body is None
        session.close()


# ---------------------------------------------------------------------------
# HMAC chain integrity tests
# ---------------------------------------------------------------------------


def test_hash_chain_link_consumes_hex_hmac():
    """AC #11: when log_event hashes ip, the chain link uses hex(ip_hmac), not raw IP."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import (
            log_event, _hash_chain_link, _ts_iso, _GENESIS_HMAC,
        )
        from sthrip.db.models import AuditLog

        session = SessionLocal()
        log_event("agent.registered", ip_address="192.0.2.1",
                  details={"agent_name": "alice"}, db=session)
        session.commit()

        row = session.query(AuditLog).first()
        assert row.ip_hmac is not None
        ip_hex = row.ip_hmac.hex()
        details_json = json.dumps(
            row.request_body, sort_keys=True, separators=(",", ":"), default=str,
        ) if row.request_body is not None else "null"
        recomputed = _hash_chain_link(
            key=_TEST_AUDIT_KEY,
            prev_hmac=row.prev_hmac,
            action=row.action,
            agent_id=str(row.agent_id) if row.agent_id else "",
            ip=ip_hex,
            ts_iso=_ts_iso(row.created_at),
            details_json=details_json,
        )
        assert row.entry_hmac == recomputed
        session.close()


def test_verify_chain_stays_valid_after_hmac_rewrite():
    """Whole chain verifies correctly when log_event is the only writer."""
    engine = _make_engine()
    SessionLocal = _session_factory(engine)

    with patch("sthrip.services.audit_logger.get_settings") as mock_settings, \
         patch("sthrip.services.audit_logger._acquire_chain_lock"), \
         patch("sthrip.services.audit_logger._release_chain_lock"):
        mock_settings.return_value.audit_hmac_key = _TEST_AUDIT_KEY
        mock_settings.return_value.environment = "dev"

        from sthrip.services.audit_logger import log_event, verify_chain

        session = SessionLocal()
        for i in range(4):
            log_event("agent.registered", ip_address=f"10.0.0.{i}",
                      details={"agent_name": f"a{i}"}, db=session)
            session.commit()

        status = verify_chain(session, key=_TEST_AUDIT_KEY)
        assert status.ok, f"chain broken: first_bad_id={status.first_bad_id}"
        assert status.total_checked == 4
        session.close()


# ---------------------------------------------------------------------------
# Migration tests — alembic round-trip on a real (sqlite) DB
# ---------------------------------------------------------------------------


def _has_postgres() -> bool:
    """Return True if a Postgres URL can be reached for migration round-trip tests."""
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return False
    try:
        eng = create_engine(url)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


_PG_AVAILABLE = _has_postgres()


def _alembic(args, env, cwd=str(_REPO_ROOT)):
    return subprocess.run(
        [str(_REPO_ROOT / ".venv" / "bin" / "alembic"), *args],
        cwd=cwd, env=env,
        capture_output=True, text=True, timeout=180,
    )


@pytest.mark.skipif(
    not _PG_AVAILABLE,
    reason="TEST_DATABASE_URL must point at Postgres for alembic round-trip "
           "(historical migrations use INET / pg_advisory_lock).",
)
def test_migration_round_trip(monkeypatch):
    """AC #9: alembic upgrade head → downgrade -1 → upgrade head succeeds."""
    env = os.environ.copy()
    env["DATABASE_URL"] = env["TEST_DATABASE_URL"]
    env["ENVIRONMENT"] = "dev"
    env.setdefault("ADMIN_API_KEY", "a" * 64)
    env["AUDIT_HMAC_KEY"] = _TEST_AUDIT_KEY
    env.setdefault("MONERO_RPC_PASS", "x")

    up1 = _alembic(["upgrade", "head"], env=env)
    assert up1.returncode == 0, f"upgrade head failed: {up1.stderr}"
    down = _alembic(["downgrade", "-1"], env=env)
    assert down.returncode == 0, f"downgrade -1 failed: {down.stderr}"
    up2 = _alembic(["upgrade", "head"], env=env)
    assert up2.returncode == 0, f"second upgrade head failed: {up2.stderr}"


@pytest.mark.skipif(
    not _PG_AVAILABLE,
    reason="TEST_DATABASE_URL must point at Postgres for backfill verification.",
)
def test_chain_remains_valid_after_migration_backfill(monkeypatch):
    """AC #10: pre-existing rows survive backfill with valid entry_hmac."""
    db_url = os.environ["TEST_DATABASE_URL"]
    env = os.environ.copy()
    env["DATABASE_URL"] = db_url
    env["ENVIRONMENT"] = "dev"
    env.setdefault("ADMIN_API_KEY", "a" * 64)
    env["AUDIT_HMAC_KEY"] = _TEST_AUDIT_KEY
    env.setdefault("MONERO_RPC_PASS", "x")

    # Step 1: roll back to a clean state, then upgrade only to PRE-our migration.
    _alembic(["downgrade", "base"], env=env)
    up_pre = _alembic(["upgrade", "p7q8r9s0t1u2"], env=env)
    assert up_pre.returncode == 0, f"upgrade pre failed: {up_pre.stderr}"

    # Step 2: insert a seeded audit_log row with raw ip_address.
    engine = create_engine(db_url)
    with engine.begin() as conn:
        genesis = hashlib.sha256(b"genesis").hexdigest()
        ts = datetime(2026, 1, 1, 0, 0, 0)
        action = "agent.registered"
        agent_id = ""
        ip = "203.0.113.42"
        details_json = "null"
        msg = "\x00".join([genesis, action, agent_id, ip, ts.isoformat(), details_json])
        entry_hmac = _hmac.new(
            _TEST_AUDIT_KEY.encode(), msg.encode(), hashlib.sha256
        ).hexdigest()
        row_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO audit_log "
                "(id, action, ip_address, created_at, prev_hmac, entry_hmac) "
                "VALUES (:id, :action, :ip, :ts, :prev, :entry)"
            ),
            {"id": row_id, "action": action, "ip": ip, "ts": ts,
             "prev": genesis, "entry": entry_hmac},
        )

    # Step 3: apply our migration.
    up_full = _alembic(["upgrade", "head"], env=env)
    assert up_full.returncode == 0, f"upgrade head failed: {up_full.stderr}"

    # Step 4: verify the chain.
    SessionLocal = _session_factory(engine)
    session = SessionLocal()
    from sthrip.services.audit_logger import verify_chain
    status = verify_chain(session, key=_TEST_AUDIT_KEY)
    assert status.ok, (
        f"chain broken after backfill: first_bad_id={status.first_bad_id}, "
        f"total_checked={status.total_checked}"
    )
    assert status.total_checked == 1
    session.close()
    engine.dispose()


def test_chain_remains_valid_after_multi_row_backfill():
    """HIGH-1 regression (iter-2): seed 3+ pre-existing F-11 rows with raw
    ip_address + valid chain, run the migration's backfill, then verify the
    full chain end-to-end.

    This test runs entirely on SQLite by constructing the pre-migration
    audit_log shape (ip_address column present alongside the new ip_hmac /
    ip_salt_id columns) and invoking the migration's pure-python
    ``_backfill_ip_hmac_and_rechain`` helper directly against the engine.

    It must FAIL on the iter-1 implementation (which only updated the
    current row's entry_hmac, orphaning row[N+1].prev_hmac) and PASS once
    the helper threads ``running_prev`` through and updates ``prev_hmac``
    along with ``entry_hmac``.
    """
    from migrations.versions.q8r9s0t1u2v3_audit_ip_hmac import (
        _backfill_ip_hmac_and_rechain,
        _compute_ip_hmac as _mig_ip_hmac,
        _hash_chain_link as _mig_link,
        _ts_iso as _mig_ts_iso,
        _GENESIS_HMAC as _MIG_GENESIS,
    )
    from sthrip.db.models import Base, AuditLog, IpSalt, Agent

    # Build a SQLite engine with the *pre-migration* audit_log shape:
    # F-11 columns (prev_hmac/entry_hmac) AND a legacy ip_address column,
    # alongside the new ip_hmac / ip_salt_id columns.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine, tables=[Agent.__table__, IpSalt.__table__, AuditLog.__table__],
    )
    # Tack on the legacy ip_address column the migration is responsible for
    # backfilling-then-dropping.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log ADD COLUMN ip_address VARCHAR(45)"))
        # Seed bootstrap salt row.
        bootstrap_id = "00000000-0000-0000-0000-000000000001"
        bootstrap_secret = b"\xab" * 32
        conn.execute(
            text("INSERT INTO ip_salts (salt_id, secret, created_at, retired_at) "
                 "VALUES (:id, :secret, :created, NULL)"),
            {"id": bootstrap_id, "secret": bootstrap_secret,
             "created": datetime(2026, 1, 1, 0, 0, 0)},
        )

    # Seed 3 audit_log rows that form a *valid F-11 chain over the raw IP*
    # (i.e. the chain shape that exists in production today before the
    # Sprint 1 migration runs).
    seeded_rows = [
        ("agent.registered", "203.0.113.1", datetime(2026, 1, 1, 0, 0, 0)),
        ("agent.registered", "203.0.113.2", datetime(2026, 1, 1, 0, 0, 1)),
        ("agent.registered", "203.0.113.3", datetime(2026, 1, 1, 0, 0, 2)),
    ]

    with engine.begin() as conn:
        prev_hmac = _MIG_GENESIS
        for action, ip, ts in seeded_rows:
            details_json = "null"
            entry_hmac = _mig_link(
                key=_TEST_AUDIT_KEY,
                prev_hmac=prev_hmac,
                action=action,
                agent_id="",
                ip=ip,                       # raw IP — pre-migration shape
                ts_iso=_mig_ts_iso(ts),
                details_json=details_json,
            )
            row_id = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO audit_log "
                    "(id, action, ip_address, created_at, prev_hmac, entry_hmac) "
                    "VALUES (:id, :action, :ip, :ts, :prev, :entry)"
                ),
                {"id": row_id, "action": action, "ip": ip, "ts": ts,
                 "prev": prev_hmac, "entry": entry_hmac},
            )
            prev_hmac = entry_hmac

    # Sanity: chain is well-formed BEFORE backfill.
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT prev_hmac, entry_hmac FROM audit_log "
                 "ORDER BY created_at ASC, id ASC")
        ).fetchall()
        assert len(rows) == 3
        running = _MIG_GENESIS
        for r in rows:
            assert r[0] == running, "pre-migration seed chain itself is broken"
            running = r[1]

    # Run the migration's backfill helper.
    with engine.begin() as conn:
        updated = _backfill_ip_hmac_and_rechain(
            conn=conn,
            bootstrap_secret=bootstrap_secret,
            bootstrap_salt_id=bootstrap_id,
            audit_hmac_key=_TEST_AUDIT_KEY,
        )
    assert updated == 3

    # Inspect post-backfill rows.
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, ip_hmac, ip_salt_id, prev_hmac, entry_hmac, "
                 "ip_address FROM audit_log "
                 "ORDER BY created_at ASC, id ASC")
        ).fetchall()

    assert len(rows) == 3, "row count must be preserved by backfill"
    # Each row has ip_hmac populated and matches the deterministic HMAC of its IP.
    for i, r in enumerate(rows):
        ip_hmac_bytes = bytes(r[1]) if r[1] is not None else None
        assert ip_hmac_bytes is not None, f"row {i} missing ip_hmac"
        assert len(ip_hmac_bytes) == 32
        expected_hmac = _mig_ip_hmac(seeded_rows[i][1], bootstrap_secret)
        assert ip_hmac_bytes == expected_hmac
        assert str(r[2]) == bootstrap_id

    # Chain integrity end-to-end (the bug under HIGH-1).
    expected_prev = _MIG_GENESIS
    for i, r in enumerate(rows):
        assert r[3] == expected_prev, (
            f"HIGH-1 regression: row {i} prev_hmac={r[3]!r} "
            f"expected={expected_prev!r} (chain orphaned by backfill)"
        )
        expected_prev = r[4]

    # And the migration's own _assert_chain_linked must accept it.
    from migrations.versions.q8r9s0t1u2v3_audit_ip_hmac import _assert_chain_linked
    with engine.begin() as conn:
        _assert_chain_linked(conn)  # raises RuntimeError on regression

    engine.dispose()


def test_assert_chain_linked_raises_on_broken_chain():
    """HIGH-2 regression: _assert_chain_linked must hard-abort the migration
    when the chain is inconsistent (catches HIGH-1 style bugs in CI)."""
    from migrations.versions.q8r9s0t1u2v3_audit_ip_hmac import (
        _assert_chain_linked, _GENESIS_HMAC as _MIG_GENESIS,
    )
    from sthrip.db.models import Base, AuditLog, IpSalt, Agent

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine, tables=[Agent.__table__, IpSalt.__table__, AuditLog.__table__],
    )

    # Seed two rows where row 2's prev_hmac does NOT match row 1's entry_hmac.
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO audit_log "
                 "(id, action, created_at, prev_hmac, entry_hmac) "
                 "VALUES (:id, :a, :t, :p, :e)"),
            {"id": str(uuid.uuid4()), "a": "agent.registered",
             "t": datetime(2026, 1, 1), "p": _MIG_GENESIS, "e": "abc" * 21 + "x"},
        )
        conn.execute(
            text("INSERT INTO audit_log "
                 "(id, action, created_at, prev_hmac, entry_hmac) "
                 "VALUES (:id, :a, :t, :p, :e)"),
            {"id": str(uuid.uuid4()), "a": "agent.registered",
             "t": datetime(2026, 1, 1, 0, 0, 1),
             "p": "deadbeef" * 8,  # WRONG — not the prior row's entry_hmac
             "e": "feedface" * 8},
        )

    with engine.begin() as conn:
        with pytest.raises(RuntimeError, match="chain integrity broken"):
            _assert_chain_linked(conn)

    engine.dispose()


def test_migration_module_imports_cleanly():
    """The new migration must be importable (syntax + symbol checks)."""
    import importlib
    mod = importlib.import_module(
        "migrations.versions.q8r9s0t1u2v3_audit_ip_hmac"
    )
    assert mod.revision == "q8r9s0t1u2v3"
    assert mod.down_revision == "p7q8r9s0t1u2"
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_migration_unit_compute_ip_hmac_matches_runtime():
    """The migration's standalone _compute_ip_hmac matches ip_salt_service.compute_ip_hmac."""
    from migrations.versions.q8r9s0t1u2v3_audit_ip_hmac import _compute_ip_hmac as mig_hmac
    from sthrip.services.ip_salt_service import compute_ip_hmac

    salt = b"\x42" * 32
    ip = "203.0.113.99"
    assert mig_hmac(ip, salt) == compute_ip_hmac(ip, salt)


def test_migration_unit_chain_link_matches_runtime():
    """The migration's _hash_chain_link must match the runtime audit_logger version."""
    from migrations.versions.q8r9s0t1u2v3_audit_ip_hmac import _hash_chain_link as mig_link
    from sthrip.services.audit_logger import _hash_chain_link as rt_link

    common = dict(
        key=_TEST_AUDIT_KEY,
        prev_hmac=hashlib.sha256(b"genesis").hexdigest(),
        action="agent.registered",
        agent_id="abc",
        ip="abcdef",
        ts_iso="2026-01-01T00:00:00",
        details_json="null",
    )
    assert mig_link(**common) == rt_link(**common)
