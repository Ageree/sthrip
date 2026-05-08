"""Sprint 5: alembic migration round-trip tests for webhook URL encryption.

We run the migration directly against an in-memory SQLite DB. The migration
script is dialect-aware enough to run on both Postgres (prod) and SQLite
(tests) -- the only Postgres-specific branch (DROP CONSTRAINT IF EXISTS)
is gated on ``conn.dialect.name``.
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from sthrip.crypto import encrypt_value, decrypt_value


_MIGRATION_PATH = (
    Path(__file__).parent.parent
    / "migrations"
    / "versions"
    / "u2v3w4x5y6z7_drop_legacy_webhook_url.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "u2v3w4x5y6z7", _MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _make_engine_with_legacy_schema():
    """Create the OLD schema (pre-Sprint-5) directly in SQLite.

    We cannot use ``Base.metadata.create_all`` because the live ORM models
    no longer have ``Agent.webhook_url`` or ``WebhookEndpoint.url``. The
    migration's job is precisely to drop those, so the test fixture has to
    mint them by hand.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE agents (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                webhook_url TEXT NULL,
                webhook_secret TEXT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE webhook_endpoints (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                url TEXT NOT NULL,
                url_encrypted TEXT NULL,
                description TEXT NULL,
                secret_encrypted TEXT NOT NULL,
                event_filters TEXT NULL,
                is_active BOOLEAN DEFAULT 1,
                failure_count INTEGER DEFAULT 0,
                disabled_at TIMESTAMP NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
    return engine


def _seed(engine, *, agent_url=None, endpoint_urls=None):
    """Insert one agent and zero or more legacy endpoints."""
    agent_id = str(uuid.uuid4())
    secret_blob = encrypt_value("whsec_seed")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agents (id, agent_name, webhook_url, webhook_secret) "
                "VALUES (:id, :name, :url, :sec)"
            ),
            {"id": agent_id, "name": "tester",
             "url": agent_url, "sec": secret_blob},
        )
        for url in endpoint_urls or []:
            conn.execute(
                text(
                    "INSERT INTO webhook_endpoints "
                    "(id, agent_id, url, secret_encrypted) "
                    "VALUES (:id, :aid, :url, :sec)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "aid": agent_id,
                    "url": url,
                    "sec": secret_blob,
                },
            )
    return agent_id


def _run_upgrade(engine, module):
    """Run module.upgrade against the engine using a real alembic op binding."""
    from alembic.runtime.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        ops = Operations(ctx)
        # Patch alembic.op global to point at our binding.
        from alembic import op as alembic_op
        alembic_op._proxy = ops
        try:
            module.upgrade()
        finally:
            alembic_op._proxy = None


def _run_downgrade(engine, module):
    from alembic.runtime.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        ops = Operations(ctx)
        from alembic import op as alembic_op
        alembic_op._proxy = ops
        try:
            module.downgrade()
        finally:
            alembic_op._proxy = None


# ─────────────────────────────────────────────────────────────────
# Schema shape after upgrade
# ─────────────────────────────────────────────────────────────────

def test_migration_drops_legacy_columns():
    engine = _make_engine_with_legacy_schema()
    _seed(engine, agent_url="https://hook/a", endpoint_urls=["https://hook/b"])
    module = _load_migration_module()

    _run_upgrade(engine, module)

    insp = sa.inspect(engine)
    agent_cols = {c["name"] for c in insp.get_columns("agents")}
    ep_cols = {c["name"] for c in insp.get_columns("webhook_endpoints")}

    assert "webhook_url" not in agent_cols
    assert "url" not in ep_cols
    assert "url_encrypted" in ep_cols


# ─────────────────────────────────────────────────────────────────
# Backfill correctness
# ─────────────────────────────────────────────────────────────────

def test_migration_backfills_existing_agent_webhook_url():
    engine = _make_engine_with_legacy_schema()
    _seed(engine, agent_url="https://agent/legacy", endpoint_urls=[])
    module = _load_migration_module()

    _run_upgrade(engine, module)

    # The migration must have created an endpoint for the legacy URL.
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT url_encrypted FROM webhook_endpoints")
        ).fetchall()
    assert len(rows) == 1
    plaintexts = [decrypt_value(r.url_encrypted) for r in rows]
    assert "https://agent/legacy" in plaintexts


def test_migration_backfills_existing_endpoint_url():
    """Plaintext webhook_endpoints.url rows get encrypted into url_encrypted."""
    engine = _make_engine_with_legacy_schema()
    _seed(engine, agent_url=None, endpoint_urls=["https://ep/one", "https://ep/two"])
    module = _load_migration_module()

    _run_upgrade(engine, module)

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT url_encrypted FROM webhook_endpoints "
                 "ORDER BY url_encrypted")
        ).fetchall()
    plaintexts = sorted(decrypt_value(r.url_encrypted) for r in rows)
    assert plaintexts == ["https://ep/one", "https://ep/two"]


def test_backfill_covers_both_sources():
    """Agent.webhook_url AND existing endpoints both get migrated."""
    engine = _make_engine_with_legacy_schema()
    _seed(
        engine,
        agent_url="https://from-agents-table/x",
        endpoint_urls=["https://from-endpoints/y"],
    )
    module = _load_migration_module()

    _run_upgrade(engine, module)

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT url_encrypted FROM webhook_endpoints")
        ).fetchall()
    plaintexts = sorted(decrypt_value(r.url_encrypted) for r in rows)
    assert plaintexts == [
        "https://from-agents-table/x",
        "https://from-endpoints/y",
    ]


def test_backfill_dedupes_same_url_already_present():
    """If agents.webhook_url == an existing endpoint url → no duplicate."""
    engine = _make_engine_with_legacy_schema()
    _seed(
        engine,
        agent_url="https://same/both",
        endpoint_urls=["https://same/both"],
    )
    module = _load_migration_module()

    _run_upgrade(engine, module)

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM webhook_endpoints")
        ).scalar()
    assert count == 1


# ─────────────────────────────────────────────────────────────────
# Idempotency / round-trip
# ─────────────────────────────────────────────────────────────────

def test_migration_round_trip():
    """upgrade → downgrade → upgrade leaves the schema in the upgraded shape."""
    engine = _make_engine_with_legacy_schema()
    _seed(engine, agent_url="https://round/trip", endpoint_urls=["https://round/trip2"])
    module = _load_migration_module()

    _run_upgrade(engine, module)
    _run_downgrade(engine, module)
    _run_upgrade(engine, module)

    insp = sa.inspect(engine)
    agent_cols = {c["name"] for c in insp.get_columns("agents")}
    ep_cols = {c["name"] for c in insp.get_columns("webhook_endpoints")}
    assert "webhook_url" not in agent_cols
    assert "url" not in ep_cols
    assert "url_encrypted" in ep_cols

    # Data integrity preserved through the round trip.
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT url_encrypted FROM webhook_endpoints")
        ).fetchall()
    decrypted = sorted(decrypt_value(r.url_encrypted) for r in rows)
    assert "https://round/trip" in decrypted
    assert "https://round/trip2" in decrypted


def test_migration_is_idempotent_on_rerun():
    """Running upgrade() twice is a no-op."""
    engine = _make_engine_with_legacy_schema()
    _seed(engine, agent_url="https://idem/url", endpoint_urls=[])
    module = _load_migration_module()

    _run_upgrade(engine, module)
    # Second run must not raise and must not duplicate rows.
    _run_upgrade(engine, module)

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM webhook_endpoints")
        ).scalar()
    assert count == 1


# ─────────────────────────────────────────────────────────────────
# Backfill abort path
# ─────────────────────────────────────────────────────────────────

def test_migration_aborts_if_backfill_incomplete():
    """If the verify step finds NULL url_encrypted, raise RuntimeError.

    Simulated by injecting a row with ``url IS NULL`` after the migration
    has already added ``url_encrypted`` (so phase 2 has nothing to encrypt
    and phase 3's verify trips).
    """
    # Build a custom schema where url is nullable to simulate corrupt state.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE agents (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                webhook_url TEXT NULL,
                webhook_secret TEXT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE webhook_endpoints (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                url TEXT NULL,
                url_encrypted TEXT NULL,
                description TEXT NULL,
                secret_encrypted TEXT NOT NULL,
                event_filters TEXT NULL,
                is_active BOOLEAN DEFAULT 1,
                failure_count INTEGER DEFAULT 0,
                disabled_at TIMESTAMP NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
    agent_id = str(uuid.uuid4())
    secret_blob = encrypt_value("whsec_seed")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO agents (id, agent_name) VALUES (:id, 'x')"),
            {"id": agent_id},
        )
        conn.execute(
            text(
                "INSERT INTO webhook_endpoints "
                "(id, agent_id, url, secret_encrypted) "
                "VALUES (:id, :aid, NULL, :sec)"
            ),
            {"id": str(uuid.uuid4()), "aid": agent_id, "sec": secret_blob},
        )

    module = _load_migration_module()

    with pytest.raises(RuntimeError, match="backfill incomplete"):
        _run_upgrade(engine, module)
