"""Sprint 4b destructive cutover: drop legacy plaintext payment-graph FKs.

Revision ID: v3w4x5y6z7a8
Revises: u2v3w4x5y6z7
Create Date: 2026-05-07

This migration drops the plaintext participant + amount columns from the
payment-graph tables (``transactions``, ``escrow_deals``,
``escrow_milestones``, ``message_relays``) so an attacker who reaches the
hub database cannot reconstruct who paid whom and how much.

By design this is the **point of no return** for the dual-write era. We
gate it behind ``STHRIP_DROP_LEGACY_FK=true`` so the migration ships
disabled and no operator runs it accidentally. When the flag is unset,
``upgrade()`` raises ``RuntimeError`` with the operator-facing
prerequisites.

Operator runbook before flipping the flag:

1. Deploy ``sthrip-op-keystore`` Railway service and verify it is
   reachable from the API service over the private network. (Sprint 4b
   ships ``railway/op-keystore-deploy/`` for this.)
2. Run ``scripts/backfill_payment_envelope.py`` until it reports zero
   NULL ``participant_envelope`` rows across all four tables.
3. Set ``STHRIP_READ_FROM_ENVELOPE=true`` and soak for at least 24 h
   in production; verify ``fallback_decrypt_error`` count is zero in
   the audit logs.
4. Only then set ``STHRIP_DROP_LEGACY_FK=true`` and run
   ``alembic upgrade head``. Once the columns are gone, the data is
   only readable through the envelope.

Idempotency: every column drop inspects the schema first so a rerun is a
no-op once the cutover has succeeded. SQLite uses ``batch_alter_table``
because it cannot drop columns natively.

Downgrade is best-effort: re-adds the dropped columns nullable, but the
plaintext data itself is unrecoverable without a database backup. The
envelope rows still hold the canonical data; restoring plaintext would
require running the backfill script in reverse.
"""
from __future__ import annotations

import logging
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger("alembic.env")

# Revision identifiers
revision = "v3w4x5y6z7a8"
down_revision = "u2v3w4x5y6z7"
branch_labels = None
depends_on = None


_FLAG_ENV = "STHRIP_DROP_LEGACY_FK"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


_PREREQ_MESSAGE = (
    "STHRIP_DROP_LEGACY_FK env not set. This migration is the destructive "
    "cutover for the payment-graph privacy hardening; do NOT run until: "
    "(1) the sthrip-op-keystore Railway service is live and reachable from "
    "the API container; (2) scripts/backfill_payment_envelope.py has run in "
    "prod and reports zero NULL participant_envelope rows across "
    "transactions, escrow_deals, escrow_milestones, and message_relays; "
    "(3) STHRIP_READ_FROM_ENVELOPE=true has been on in prod for at least "
    "24h with zero fallback_decrypt_error in the audit log. "
    "Then set STHRIP_DROP_LEGACY_FK=true and rerun "
    "`alembic upgrade head`."
)


_DROP_PLAN = (
    ("transactions", ("from_agent_id", "to_agent_id", "amount")),
    ("escrow_deals", ("buyer_id", "seller_id", "amount")),
    ("escrow_milestones", ("amount",)),
    ("message_relays", ("from_agent_id", "to_agent_id")),
)


_RESTORE_PLAN = (
    (
        "transactions",
        (
            ("from_agent_id", lambda: sa.Column("from_agent_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True)),
            ("to_agent_id", lambda: sa.Column("to_agent_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True)),
            ("amount", lambda: sa.Column("amount", sa.Numeric(20, 12), nullable=True)),
        ),
    ),
    (
        "escrow_deals",
        (
            ("buyer_id", lambda: sa.Column("buyer_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True)),
            ("seller_id", lambda: sa.Column("seller_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True)),
            ("amount", lambda: sa.Column("amount", sa.Numeric(20, 12), nullable=True)),
        ),
    ),
    (
        "escrow_milestones",
        (
            ("amount", lambda: sa.Column("amount", sa.Numeric(20, 12), nullable=True)),
        ),
    ),
    (
        "message_relays",
        (
            ("from_agent_id", lambda: sa.Column("from_agent_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True)),
            ("to_agent_id", lambda: sa.Column("to_agent_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True)),
        ),
    ),
)


def _flag_enabled() -> bool:
    raw = os.environ.get(_FLAG_ENV, "")
    return raw.strip().lower() in _TRUTHY


def _column_names(insp, table: str) -> set:
    try:
        return {col["name"] for col in insp.get_columns(table)}
    except Exception:  # noqa: BLE001 — table may not exist on slim test schemas
        return set()


def _table_exists(insp, table: str) -> bool:
    try:
        return table in insp.get_table_names()
    except Exception:  # noqa: BLE001 — defensive on exotic dialects
        return False


def upgrade() -> None:
    if not _flag_enabled():
        raise RuntimeError(_PREREQ_MESSAGE)

    conn = op.get_bind()
    insp = sa_inspect(conn)

    for table, columns in _DROP_PLAN:
        if not _table_exists(insp, table):
            logger.info("v3w4x5y6z7a8: table %s missing; skipping", table)
            continue
        present = _column_names(insp, table)
        to_drop = [c for c in columns if c in present]
        if not to_drop:
            logger.info("v3w4x5y6z7a8: %s already cleaned; skipping", table)
            continue
        with op.batch_alter_table(table) as batch:
            for col in to_drop:
                batch.drop_column(col)
        logger.info("v3w4x5y6z7a8: dropped %s.%s", table, ",".join(to_drop))
        # Refresh inspector after each table — batch_alter_table on SQLite
        # rebuilds the table and old reflected metadata is stale.
        insp = sa_inspect(conn)


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa_inspect(conn)

    for table, restorations in _RESTORE_PLAN:
        if not _table_exists(insp, table):
            logger.info("v3w4x5y6z7a8 downgrade: table %s missing; skipping", table)
            continue
        present = _column_names(insp, table)
        missing = [(name, factory) for name, factory in restorations if name not in present]
        if not missing:
            continue
        with op.batch_alter_table(table) as batch:
            for _name, factory in missing:
                batch.add_column(factory())
        logger.info(
            "v3w4x5y6z7a8 downgrade: re-added %s columns: %s",
            table,
            ",".join(name for name, _ in missing),
        )
        insp = sa_inspect(conn)
