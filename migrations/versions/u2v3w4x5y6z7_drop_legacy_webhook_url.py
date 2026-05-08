"""Sprint 5 (anonymity-hardening): drop legacy webhook_url, encrypt URLs.

Revision ID: u2v3w4x5y6z7
Revises: s0t1u2v3w4x5
Create Date: 2026-05-06

Migration steps
---------------
1. ADD ``webhook_endpoints.url_encrypted TEXT NULL``  (idempotent: skip if
   already present).
2. Backfill, in-Python, batched:
   * existing ``webhook_endpoints`` rows with plaintext ``url`` →
     encrypt and write to ``url_encrypted`` when not yet set.
   * ``agents`` rows with non-null ``webhook_url`` not yet covered by an
     endpoint → insert a synthetic endpoint row (encrypted URL +
     reused ``agents.webhook_secret`` if present, else mint a fresh one
     and encrypt it).
3. Verify: 0 rows have ``url_encrypted IS NULL`` after backfill.
   Raise RuntimeError if any remain (operator must intervene).
4. ALTER ``url_encrypted`` SET NOT NULL.
5. Drop unique constraint ``uq_agent_webhook_url`` (Postgres).
   On SQLite this constraint is implicit and dropping the ``url`` column
   later via ``batch_alter_table`` recreates the table without it.
6. Drop ``webhook_endpoints.url`` IF EXISTS.
7. Drop ``agents.webhook_url`` IF EXISTS.

Idempotency: each step inspects the schema first. Re-running the upgrade
is a no-op once it has succeeded once.

Downgrade: best-effort. Re-adds the plaintext columns and decrypts each
``url_encrypted`` back into ``url`` (and the first endpoint URL into
``agents.webhook_url`` per agent). Aborts loudly if the Fernet key
differs and decryption fails.
"""
from __future__ import annotations

import logging
import os
import secrets

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.sql import text

logger = logging.getLogger("alembic.env")

# Revision identifiers
revision = "u2v3w4x5y6z7"
down_revision = "s0t1u2v3w4x5"
branch_labels = None
depends_on = None


def _column_names(insp, table: str) -> set:
    return {col["name"] for col in insp.get_columns(table)}


def _has_column(insp, table: str, name: str) -> bool:
    return name in _column_names(insp, table)


def _get_fernet():
    """Resolve Fernet using same logic as ``sthrip.crypto._get_fernet``.

    Imported lazily so alembic can import this file without booting the
    whole sthrip package on environments missing optional deps.
    """
    from sthrip.crypto import _get_fernet  # noqa: WPS433
    return _get_fernet()


def _generate_secret_blob(fernet) -> str:
    """Mint a fresh ``whsec_...`` and return the Fernet-encrypted token."""
    plaintext = f"whsec_{secrets.token_hex(24)}"
    return fernet.encrypt(plaintext.encode()).decode()


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    # ── Phase 1: add nullable url_encrypted ────────────────────────────────
    if not _has_column(insp, "webhook_endpoints", "url_encrypted"):
        op.add_column(
            "webhook_endpoints",
            sa.Column("url_encrypted", sa.Text(), nullable=True),
        )
        insp = sa_inspect(conn)
        logger.info("Added webhook_endpoints.url_encrypted (nullable)")

    fernet = _get_fernet()

    # ── Phase 2a: encrypt legacy plaintext webhook_endpoints.url ───────────
    if _has_column(insp, "webhook_endpoints", "url"):
        rows = conn.execute(
            text(
                "SELECT id, url FROM webhook_endpoints "
                "WHERE url_encrypted IS NULL AND url IS NOT NULL"
            )
        ).fetchall()
        for row in rows:
            ciphertext = fernet.encrypt(str(row.url).encode()).decode()
            conn.execute(
                text(
                    "UPDATE webhook_endpoints SET url_encrypted = :enc "
                    "WHERE id = :id"
                ),
                {"enc": ciphertext, "id": row.id},
            )
        if rows:
            logger.info(
                "Backfilled %d webhook_endpoints rows from plaintext url",
                len(rows),
            )

    # ── Phase 2b: backfill from agents.webhook_url ─────────────────────────
    if _has_column(insp, "agents", "webhook_url"):
        agent_rows = conn.execute(
            text(
                "SELECT id, webhook_url, webhook_secret FROM agents "
                "WHERE webhook_url IS NOT NULL"
            )
        ).fetchall()
        for arow in agent_rows:
            agent_id = arow.id
            url_plain = str(arow.webhook_url)
            # Skip if any existing endpoint for this agent already encrypts
            # the same URL.
            existing = conn.execute(
                text(
                    "SELECT id, url_encrypted FROM webhook_endpoints "
                    "WHERE agent_id = :aid"
                ),
                {"aid": agent_id},
            ).fetchall()
            already_present = False
            for erow in existing:
                if not erow.url_encrypted:
                    continue
                try:
                    if fernet.decrypt(erow.url_encrypted.encode()).decode() == url_plain:
                        already_present = True
                        break
                except Exception:
                    continue
            if already_present:
                continue
            secret_blob = arow.webhook_secret or _generate_secret_blob(fernet)
            url_enc = fernet.encrypt(url_plain.encode()).decode()
            # Populate the legacy `url` column too if it still exists at
            # this point (it gets dropped in Phase 6). Some prod schemas
            # keep ``url NOT NULL``, so we must satisfy it.
            insp_local = sa_inspect(conn)
            includes_legacy_url = _has_column(
                insp_local, "webhook_endpoints", "url"
            )
            insert_kwargs = {
                "id": _new_uuid(conn),
                "agent_id": agent_id,
                "url_enc": url_enc,
                "secret_enc": secret_blob,
                "description": "legacy-backfill",
                "filters": None,
                "is_active": True,
                "failure_count": 0,
            }
            if includes_legacy_url:
                insert_kwargs["url_plain"] = url_plain
                conn.execute(
                    text(
                        "INSERT INTO webhook_endpoints "
                        "(id, agent_id, url, url_encrypted, secret_encrypted, "
                        " description, event_filters, is_active, failure_count, "
                        " created_at, updated_at) "
                        "VALUES "
                        "(:id, :agent_id, :url_plain, :url_enc, :secret_enc, "
                        " :description, :filters, :is_active, :failure_count, "
                        f" {_now_sql(conn)}, {_now_sql(conn)})"
                    ),
                    insert_kwargs,
                )
            else:
                conn.execute(
                    text(
                        "INSERT INTO webhook_endpoints "
                        "(id, agent_id, url_encrypted, secret_encrypted, "
                        " description, event_filters, is_active, failure_count, "
                        " created_at, updated_at) "
                        "VALUES "
                        "(:id, :agent_id, :url_enc, :secret_enc, "
                        " :description, :filters, :is_active, :failure_count, "
                        f" {_now_sql(conn)}, {_now_sql(conn)})"
                    ),
                    insert_kwargs,
                )
        if agent_rows:
            logger.info(
                "Backfilled %d agents.webhook_url values into webhook_endpoints",
                len(agent_rows),
            )

    # ── Phase 3: verify no NULL url_encrypted remains ──────────────────────
    null_count = conn.execute(
        text("SELECT COUNT(*) FROM webhook_endpoints WHERE url_encrypted IS NULL")
    ).scalar()
    if null_count:
        raise RuntimeError(
            f"u2v3w4x5y6z7 backfill incomplete: {null_count} webhook_endpoints "
            "rows still have NULL url_encrypted. Aborting before destructive drops."
        )

    # ── Phase 4: ALTER url_encrypted NOT NULL ──────────────────────────────
    insp = sa_inspect(conn)
    cols = {col["name"]: col for col in insp.get_columns("webhook_endpoints")}
    if cols.get("url_encrypted") and cols["url_encrypted"].get("nullable", True):
        with op.batch_alter_table("webhook_endpoints") as batch:
            batch.alter_column("url_encrypted", nullable=False)
        logger.info("Set webhook_endpoints.url_encrypted NOT NULL")

    # ── Phase 5: drop legacy unique constraint (Postgres only) ─────────────
    dialect = conn.dialect.name
    if dialect == "postgresql":
        # ``IF EXISTS`` keeps it idempotent across reruns and across envs
        # that may have already been migrated by hand.
        conn.execute(
            text(
                "ALTER TABLE webhook_endpoints "
                "DROP CONSTRAINT IF EXISTS uq_agent_webhook_url"
            )
        )

    # ── Phase 6: drop webhook_endpoints.url ────────────────────────────────
    insp = sa_inspect(conn)
    if _has_column(insp, "webhook_endpoints", "url"):
        with op.batch_alter_table("webhook_endpoints") as batch:
            batch.drop_column("url")
        logger.info("Dropped webhook_endpoints.url")

    # ── Phase 7: drop agents.webhook_url ───────────────────────────────────
    insp = sa_inspect(conn)
    if _has_column(insp, "agents", "webhook_url"):
        with op.batch_alter_table("agents") as batch:
            batch.drop_column("webhook_url")
        logger.info("Dropped agents.webhook_url")


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    # Re-add plaintext columns nullable
    if not _has_column(insp, "agents", "webhook_url"):
        op.add_column(
            "agents",
            sa.Column("webhook_url", sa.Text(), nullable=True),
        )
        insp = sa_inspect(conn)
    if not _has_column(insp, "webhook_endpoints", "url"):
        op.add_column(
            "webhook_endpoints",
            sa.Column("url", sa.String(length=2048), nullable=True),
        )
        insp = sa_inspect(conn)

    # Decrypt url_encrypted back into both plaintext columns
    fernet = _get_fernet()
    rows = conn.execute(
        text(
            "SELECT id, agent_id, url_encrypted FROM webhook_endpoints "
            "WHERE url_encrypted IS NOT NULL"
        )
    ).fetchall()
    seen_agents = set()
    for row in rows:
        try:
            plaintext = fernet.decrypt(row.url_encrypted.encode()).decode()
        except Exception as exc:  # pragma: no cover -- key mismatch path
            raise RuntimeError(
                f"Cannot decrypt url_encrypted for endpoint {row.id} during "
                "downgrade -- WEBHOOK_ENCRYPTION_KEY mismatch?"
            ) from exc
        conn.execute(
            text("UPDATE webhook_endpoints SET url = :url WHERE id = :id"),
            {"url": plaintext, "id": row.id},
        )
        if row.agent_id not in seen_agents:
            conn.execute(
                text("UPDATE agents SET webhook_url = :url WHERE id = :id"),
                {"url": plaintext, "id": row.agent_id},
            )
            seen_agents.add(row.agent_id)

    # Restore NOT NULL on url, drop url_encrypted
    insp = sa_inspect(conn)
    cols = {col["name"]: col for col in insp.get_columns("webhook_endpoints")}
    if cols.get("url") and cols["url"].get("nullable", True):
        with op.batch_alter_table("webhook_endpoints") as batch:
            batch.alter_column("url", nullable=False)
    if _has_column(insp, "webhook_endpoints", "url_encrypted"):
        with op.batch_alter_table("webhook_endpoints") as batch:
            batch.drop_column("url_encrypted")


# ─────────────────────────────────────────────────────────────────────────
# Dialect-aware helpers
# ─────────────────────────────────────────────────────────────────────────

def _new_uuid(conn) -> str:
    """Generate a fresh UUID as a string -- Postgres + SQLite friendly."""
    import uuid as _uuid
    return str(_uuid.uuid4())


def _now_sql(conn) -> str:
    """SQL fragment for ``current timestamp``, dialect-aware."""
    if conn.dialect.name == "postgresql":
        return "NOW()"
    return "CURRENT_TIMESTAMP"
