"""Auto-purge service (Phase 2 Sprint 1).

Daily background job that deletes terminal-state records older than the
configured retention window from the four payment-graph tables and the
audit log. Each run writes one ``purge_metadata`` row recording per-table
counts and (when audit_log was touched) the synthetic "chain reset" row id.

Design notes
------------
* **Terminal status only.** ``transactions``, ``escrow_deals``, and
  ``escrow_milestones`` rows are deleted only when their status is in the
  terminal set defined per model. A PENDING/ACCEPTED/DELIVERED row is never
  swept even if it is older than the retention window; an active deal must
  finish (or expire) first.
* **FK protection.** A transaction referenced by an active (non-terminal)
  escrow deal is NOT deleted, even if its own status would otherwise allow
  it. The check uses an EXISTS subquery so the database short-circuits.
* **Message relays** have no terminal status; they are ephemeral by design
  and any row past the retention window is fair game.
* **HMAC chain rolling reset** (audit_log specifically): rather than
  refusing to purge audit history (which would defeat the point of a
  retention policy), we delete eligible rows AND insert a synthetic
  ``chain_reset`` row whose ``prev_hmac`` is NULL. ``verify_chain`` treats
  rows with NULL ``entry_hmac`` as legacy (skipped), so the new chain
  starts fresh from the next real event. The audit_log row id (UUID
  stringified) is recorded in ``purge_metadata.audit_chain_reset_at_id``.

Concurrency
-----------
The orchestrator (`run_full_purge`) acquires no distributed lock by design
— the scheduler tier already serialises this job (see ``api/main_v2.py``,
``_purge_loop``). Each phase runs inside its own ``with get_db()`` block
to keep the transaction window short.

Testability
-----------
Every function accepts an explicit ``db: Session`` so tests can drive the
service against an in-memory SQLite engine without monkeypatching get_db.
The orchestrator falls back to ``get_db()`` only when called without an
explicit session — the production cron path.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session

from sthrip.db.models import (
    AuditLog,
    CanaryState,  # noqa: F401 — kept for service-module symmetry
    EscrowDeal,
    EscrowMilestone,
    MessageRelay,
    PurgeMetadata,
    Transaction,
)
from sthrip.db.enums import EscrowStatus, MilestoneStatus, TransactionStatus

logger = logging.getLogger("sthrip.purge")

# Synthetic action stored on the chain-reset row. The value is fixed so
# operators (and the contract test) can locate it by string match.
_CHAIN_RESET_ACTION = "chain_reset"

# Status sets considered terminal for purge eligibility. Defined as
# module-level frozensets so callers and tests can introspect them, and
# adjustments are localised.
TRANSACTION_TERMINAL_STATUSES: frozenset = frozenset({
    TransactionStatus.CONFIRMED,
    TransactionStatus.FAILED,
    TransactionStatus.ORPHANED,
})

ESCROW_TERMINAL_STATUSES: frozenset = frozenset({
    EscrowStatus.COMPLETED,
    EscrowStatus.CANCELLED,
    EscrowStatus.EXPIRED,
})

MILESTONE_TERMINAL_STATUSES: frozenset = frozenset({
    MilestoneStatus.COMPLETED,
    MilestoneStatus.CANCELLED,
    MilestoneStatus.EXPIRED,
})


def _utc_now() -> datetime:
    """Return tz-aware UTC now.

    SQLite's DateTime columns are stored naive; comparisons in WHERE clauses
    work correctly because SQLAlchemy strips tzinfo before binding.
    """
    return datetime.now(timezone.utc)


def _cutoff(retention_days: int, *, now: Optional[datetime] = None) -> datetime:
    """Compute the inclusive lower bound for retention.

    A row is purge-eligible when ``created_at < cutoff``.
    """
    if retention_days < 1:
        raise ValueError(
            f"retention_days must be >= 1 (got {retention_days})"
        )
    base = now if now is not None else _utc_now()
    return base - timedelta(days=retention_days)


# ---------------------------------------------------------------------------
# Per-table purge functions
# ---------------------------------------------------------------------------

def purge_transactions(
    retention_days: int,
    *,
    db: Session,
    now: Optional[datetime] = None,
) -> int:
    """Delete terminal-state ``transactions`` rows older than the cutoff.

    A row is skipped when an active (non-terminal) ``escrow_deals`` row
    references the same buyer/seller pair, mirroring the contractual
    expectation that on-chain history backing an undelivered escrow stays
    on disk until the escrow itself terminates.

    Returns the number of rows deleted.
    """
    cutoff = _cutoff(retention_days, now=now)

    # Subquery: an EscrowDeal in non-terminal status whose buyer or seller
    # equals the transaction's from/to agent. Because the schema has no
    # direct transaction → escrow FK, we conservatively treat any active
    # deal touching either participant as a reference. False positives
    # delay deletion by one cycle; false negatives would violate the
    # privacy invariant.
    active_escrow_for_tx = exists().where(
        and_(
            EscrowDeal.status.notin_(list(ESCROW_TERMINAL_STATUSES)),
            (
                (EscrowDeal.buyer_id == Transaction.from_agent_id)
                | (EscrowDeal.buyer_id == Transaction.to_agent_id)
                | (EscrowDeal.seller_id == Transaction.from_agent_id)
                | (EscrowDeal.seller_id == Transaction.to_agent_id)
            ),
        )
    )

    candidates = (
        db.query(Transaction)
        .filter(Transaction.created_at < cutoff)
        .filter(Transaction.status.in_(list(TRANSACTION_TERMINAL_STATUSES)))
        .filter(~active_escrow_for_tx)
        .all()
    )

    deleted = 0
    for row in candidates:
        db.delete(row)
        deleted += 1

    if deleted:
        logger.info("purge_transactions: deleted %d rows", deleted)
    return deleted


def purge_escrow_deals(
    retention_days: int,
    *,
    db: Session,
    now: Optional[datetime] = None,
) -> int:
    """Delete terminal-state ``escrow_deals`` rows older than the cutoff.

    Cascades to ``escrow_milestones`` via the existing
    ``ondelete="CASCADE"`` FK. Milestone counters returned from
    ``purge_escrow_milestones`` only cover orphaned-by-deal milestones —
    cascade deletions are NOT counted there.
    """
    cutoff = _cutoff(retention_days, now=now)

    candidates = (
        db.query(EscrowDeal)
        .filter(EscrowDeal.created_at < cutoff)
        .filter(EscrowDeal.status.in_(list(ESCROW_TERMINAL_STATUSES)))
        .all()
    )

    deleted = 0
    for row in candidates:
        db.delete(row)
        deleted += 1

    if deleted:
        logger.info("purge_escrow_deals: deleted %d rows", deleted)
    return deleted


def purge_escrow_milestones(
    retention_days: int,
    *,
    db: Session,
    now: Optional[datetime] = None,
) -> int:
    """Delete terminal-state ``escrow_milestones`` rows older than the cutoff.

    Used to mop up milestones whose parent deal is still active but whose
    own state machine has terminated (e.g. EXPIRED in a long-running
    multi-milestone deal).
    """
    cutoff = _cutoff(retention_days, now=now)

    # SQLite's reflected schema for escrow_milestones lacks a ``created_at``
    # column — deadlines are stored relative to the parent deal. We use
    # ``activated_at`` as the closest proxy: a milestone whose terminal
    # transition happened more than ``retention_days`` ago is fair game.
    # Rows that never activated are not eligible.
    candidates = (
        db.query(EscrowMilestone)
        .filter(EscrowMilestone.activated_at.isnot(None))
        .filter(EscrowMilestone.activated_at < cutoff)
        .filter(EscrowMilestone.status.in_(list(MILESTONE_TERMINAL_STATUSES)))
        .all()
    )

    deleted = 0
    for row in candidates:
        db.delete(row)
        deleted += 1

    if deleted:
        logger.info("purge_escrow_milestones: deleted %d rows", deleted)
    return deleted


def purge_message_relays(
    retention_days: int,
    *,
    db: Session,
    now: Optional[datetime] = None,
) -> int:
    """Delete ``message_relays`` rows older than the cutoff.

    Message relays are ephemeral by design (the schema even carries an
    ``expires_at`` column). No status guard is applied — every row past the
    retention window is purgeable.
    """
    cutoff = _cutoff(retention_days, now=now)

    candidates = (
        db.query(MessageRelay)
        .filter(MessageRelay.created_at < cutoff)
        .all()
    )

    deleted = 0
    for row in candidates:
        db.delete(row)
        deleted += 1

    if deleted:
        logger.info("purge_message_relays: deleted %d rows", deleted)
    return deleted


def purge_audit_log(
    retention_days: int,
    *,
    db: Session,
    now: Optional[datetime] = None,
) -> dict:
    """Delete audit_log rows older than the cutoff and emit a chain reset.

    The HMAC chain is rolling-reset rather than broken: we insert a
    synthetic ``chain_reset`` row whose ``prev_hmac=NULL`` and
    ``entry_hmac=NULL`` AFTER deleting eligible rows. ``verify_chain``
    skips legacy rows (NULL entry_hmac), so the post-purge chain restarts
    from the next real event seamlessly.

    Returns a dict ``{"deleted": int, "reset_row_id": str | None}``. The
    ``reset_row_id`` is the stringified UUID of the synthetic row, or None
    if nothing was deleted (no reset needed).
    """
    base_now = now if now is not None else _utc_now()
    cutoff = _cutoff(retention_days, now=base_now)

    candidates = (
        db.query(AuditLog)
        .filter(AuditLog.created_at < cutoff)
        .all()
    )

    deleted = len(candidates)
    if deleted == 0:
        return {"deleted": 0, "reset_row_id": None}

    for row in candidates:
        db.delete(row)

    # Flush so deletes hit the session before the synthetic row is added —
    # keeps the HEAD invariant simple under SQLite.
    db.flush()

    reset_id = uuid4()
    reset_row = AuditLog(
        id=reset_id,
        agent_id=None,
        action=_CHAIN_RESET_ACTION,
        resource_type=None,
        resource_id=None,
        ip_hmac=None,
        ip_salt_id=None,
        request_method=None,
        request_path=None,
        request_body=None,
        success=True,
        error_message=None,
        created_at=base_now,
        # Both HMACs NULL: verify_chain treats NULL entry_hmac as legacy
        # and skips. This row is a marker, not a chain link.
        prev_hmac=None,
        entry_hmac=None,
    )
    db.add(reset_row)
    db.flush()

    logger.info(
        "purge_audit_log: deleted %d rows; chain_reset row id=%s",
        deleted,
        reset_id,
    )
    return {"deleted": deleted, "reset_row_id": str(reset_id)}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_full_purge(
    retention_days: int,
    *,
    db: Optional[Session] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Run all purge phases sequentially and write a ``purge_metadata`` row.

    Returns a summary dict containing per-table delete counts and the
    metadata row id. Caller-provided ``db`` is used end-to-end (single
    transaction). Without ``db``, a fresh ``get_db()`` session is opened.

    The order of phases is deliberate: terminal escrow_deals are purged
    before transactions, so transaction rows referenced by deals about to
    be deleted (now non-existent in the active set) become eligible for
    deletion in the same run rather than the next cycle.
    """
    if db is not None:
        return _run_full_purge_with_db(retention_days, db, now=now)

    from sthrip.db.database import get_db
    with get_db() as session:
        summary = _run_full_purge_with_db(retention_days, session, now=now)
        session.commit()
        return summary


def _run_full_purge_with_db(
    retention_days: int,
    db: Session,
    *,
    now: Optional[datetime] = None,
) -> dict:
    base_now = now if now is not None else _utc_now()

    deals_deleted = purge_escrow_deals(retention_days, db=db, now=base_now)
    milestones_deleted = purge_escrow_milestones(retention_days, db=db, now=base_now)
    txs_deleted = purge_transactions(retention_days, db=db, now=base_now)
    relays_deleted = purge_message_relays(retention_days, db=db, now=base_now)
    audit_result = purge_audit_log(retention_days, db=db, now=base_now)

    metadata = PurgeMetadata(
        run_at=base_now,
        transactions_deleted=txs_deleted,
        escrow_deals_deleted=deals_deleted,
        escrow_milestones_deleted=milestones_deleted,
        message_relays_deleted=relays_deleted,
        audit_log_deleted=audit_result["deleted"],
        audit_chain_reset_at_id=audit_result["reset_row_id"],
    )
    db.add(metadata)
    db.flush()

    summary = {
        "run_at": base_now.isoformat(),
        "transactions_deleted": txs_deleted,
        "escrow_deals_deleted": deals_deleted,
        "escrow_milestones_deleted": milestones_deleted,
        "message_relays_deleted": relays_deleted,
        "audit_log_deleted": audit_result["deleted"],
        "audit_chain_reset_at_id": audit_result["reset_row_id"],
        "metadata_id": metadata.id,
    }
    logger.info("run_full_purge: %s", summary)
    return summary
