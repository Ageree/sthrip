"""Sprint 4a: rerun-safe payment-envelope backfill.

Walks the four payment-graph tables and writes a participant_envelope
(plus amount_bucket where applicable) for any row whose envelope is
NULL. Skips rows that already have an envelope so the script is safe to
re-execute, including via cron.

Usage
-----

    # Dry-run: report counts only.
    python scripts/backfill_payment_envelope.py --dry-run

    # Real run, default batch of 500.
    python scripts/backfill_payment_envelope.py

    # Limit to one table (useful for staging staged rollouts).
    python scripts/backfill_payment_envelope.py --table escrow_deals

    # Smaller batches for write-pressure-sensitive prod windows.
    python scripts/backfill_payment_envelope.py --batch-size 100

The script exits non-zero if any batch fails. Successful runs print one
line per table with ``processed=N skipped=N`` totals.

Idempotency
-----------

The SQL filter ``WHERE participant_envelope IS NULL`` skips every row
that's already been backfilled. Re-running the script is a no-op once
all rows are covered. This makes it safe as a Railway cron entry —
schedule it every 10 min for the first few hours after deploy, then
remove once the row counts are zero.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from typing import Iterable, List, Optional

# Project root — make `sthrip` importable when running directly.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from sthrip.db import models  # noqa: E402
from sthrip.services.payment_envelope_writer import apply_envelope  # noqa: E402

logger = logging.getLogger("backfill_payment_envelope")


# ---------------------------------------------------------------------------
# Table descriptors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableSpec:
    """Mapping from table name to the model and FK accessors we use."""

    name: str
    model: type
    from_attr: str
    to_attr: str
    amount_attr: Optional[str]
    description_attr: Optional[str]


_SPECS: List[TableSpec] = [
    TableSpec(
        name="transactions",
        model=models.Transaction,
        from_attr="from_agent_id",
        to_attr="to_agent_id",
        amount_attr="amount",
        description_attr="memo",
    ),
    TableSpec(
        name="escrow_deals",
        model=models.EscrowDeal,
        from_attr="buyer_id",
        to_attr="seller_id",
        amount_attr="amount",
        description_attr="description",
    ),
    TableSpec(
        name="escrow_milestones",
        model=models.EscrowMilestone,
        from_attr="",  # filled in from parent escrow at write time
        to_attr="",
        amount_attr="amount",
        description_attr="description",
    ),
    TableSpec(
        name="message_relays",
        model=models.MessageRelay,
        from_attr="from_agent_id",
        to_attr="to_agent_id",
        amount_attr=None,  # message relays carry no amount
        description_attr="payment_id",
    ),
]


# ---------------------------------------------------------------------------
# Per-row backfill
# ---------------------------------------------------------------------------


def _resolve_milestone_parents(
    session: Session,
    milestones: Iterable[models.EscrowMilestone],
) -> dict:
    """Pre-fetch parent escrow buyer/seller for a batch of milestones.

    Avoids one query per milestone — bounded by the batch size. Returns
    a map ``{escrow_id: (buyer_id, seller_id)}``.
    """
    escrow_ids = {ms.escrow_id for ms in milestones}
    if not escrow_ids:
        return {}
    parents = (
        session.query(models.EscrowDeal)
        .filter(models.EscrowDeal.id.in_(escrow_ids))
        .all()
    )
    return {p.id: (p.buyer_id, p.seller_id) for p in parents}


def _backfill_row(
    session: Session,
    spec: TableSpec,
    row: object,
    parent_map: dict,
    *,
    dry_run: bool,
) -> bool:
    """Compute and apply envelope for a single row. Returns True if mutated.

    Idempotency is enforced both by the SQL filter (envelope IS NULL) and
    by ``apply_envelope`` itself, which short-circuits when a row already
    has a non-null envelope.
    """
    if getattr(row, "participant_envelope", None) is not None:
        return False

    if spec.name == "escrow_milestones":
        parent = parent_map.get(getattr(row, "escrow_id", None))
        if parent is None:
            logger.warning(
                "milestone %s has no parent escrow; skipping",
                getattr(row, "id", "<unknown>"),
            )
            return False
        from_id, to_id = parent
    else:
        from_id = getattr(row, spec.from_attr, None)
        to_id = getattr(row, spec.to_attr, None)

    amount = getattr(row, spec.amount_attr, None) if spec.amount_attr else None
    description = (
        getattr(row, spec.description_attr, None)
        if spec.description_attr
        else None
    )

    if dry_run:
        # Don't mutate; just report this row would be backfilled.
        return True

    apply_envelope(
        row,
        from_agent_id=from_id,
        to_agent_id=to_id,
        amount=amount,
        description=description,
    )
    return True


# ---------------------------------------------------------------------------
# Per-table loop
# ---------------------------------------------------------------------------


def backfill_table(
    session: Session,
    spec: TableSpec,
    *,
    batch_size: int,
    dry_run: bool,
) -> dict:
    """Backfill one table. Returns ``{processed, skipped}`` counts.

    Streams rows in deterministic batches of ``batch_size`` ordered by
    primary key, committing after each batch. The SQL filter ensures
    re-runs skip already-backfilled rows.
    """
    processed = 0
    skipped = 0
    while True:
        rows = (
            session.query(spec.model)
            .filter(spec.model.participant_envelope.is_(None))
            .order_by(spec.model.id)
            .limit(batch_size)
            .all()
        )
        if not rows:
            break

        parent_map = (
            _resolve_milestone_parents(session, rows)  # type: ignore[arg-type]
            if spec.name == "escrow_milestones"
            else {}
        )

        for row in rows:
            if _backfill_row(session, spec, row, parent_map, dry_run=dry_run):
                processed += 1
            else:
                skipped += 1

        if dry_run:
            # No commit on dry-run; also break to avoid an infinite loop
            # since we never mutated participant_envelope.
            break

        session.commit()

        if len(rows) < batch_size:
            break

    return {"processed": processed, "skipped": skipped}


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


def run_backfill(
    session: Session,
    *,
    table_filter: Optional[str] = None,
    batch_size: int = 500,
    dry_run: bool = False,
) -> dict:
    """Run the backfill for all (or one) table. Pure function over a session.

    Tests inject a session bound to an in-memory SQLite. CLI entrypoint
    builds a session from ``DATABASE_URL``.
    """
    results: dict = {}
    for spec in _SPECS:
        if table_filter and spec.name != table_filter:
            continue
        try:
            stats = backfill_table(
                session, spec, batch_size=batch_size, dry_run=dry_run,
            )
        except Exception:
            logger.exception("backfill failed for table=%s", spec.name)
            session.rollback()
            raise
        results[spec.name] = stats
        logger.info(
            "table=%s processed=%d skipped=%d (dry_run=%s)",
            spec.name, stats["processed"], stats["skipped"], dry_run,
        )
    return results


def _make_session() -> Session:
    """Build a session from ``DATABASE_URL``. CLI-only path."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit(
            "DATABASE_URL is not set; backfill cannot connect to the DB."
        )
    engine = create_engine(db_url)
    Session_ = sessionmaker(bind=engine, expire_on_commit=False)
    return Session_()


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sprint 4a payment-envelope backfill (rerun-safe).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report counts only; do not write envelopes.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=500,
        help="Rows per commit (default 500).",
    )
    parser.add_argument(
        "--table", choices=[s.name for s in _SPECS], default=None,
        help="Limit to a single table.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    session = _make_session()
    try:
        results = run_backfill(
            session,
            table_filter=args.table,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
    finally:
        session.close()

    print("backfill complete:")
    for table, stats in results.items():
        print(f"  {table}: processed={stats['processed']} skipped={stats['skipped']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
