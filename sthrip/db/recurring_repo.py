"""
RecurringPaymentRepository — data-access layer for RecurringPayment records.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import or_, desc
from sqlalchemy.orm import Session

from . import models
from .models import RecurringPayment, RecurringInterval
from ._repo_base import _MAX_QUERY_LIMIT


class RecurringPaymentRepository:
    """Data access for server-side recurring payment schedules."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        from_agent_id: UUID,
        to_agent_id: UUID,
        amount: Decimal,
        interval: RecurringInterval,
        max_payments: Optional[int],
        next_payment_at: datetime,
    ) -> RecurringPayment:
        """Create a new active recurring payment schedule."""
        payment = RecurringPayment(
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            amount=amount,
            interval=interval,
            max_payments=max_payments,
            next_payment_at=next_payment_at,
            is_active=True,
            payments_made=0,
            total_paid=Decimal("0"),
        )
        self.db.add(payment)
        self.db.flush()
        return payment

    def get_by_id(self, payment_id: UUID) -> Optional[RecurringPayment]:
        """Return the RecurringPayment with the given ID, or None."""
        return (
            self.db.query(RecurringPayment)
            .filter(RecurringPayment.id == payment_id)
            .first()
        )

    def get_due_payments(self) -> List[RecurringPayment]:
        """Return all active payments whose next_payment_at is now or in the past."""
        now = datetime.now(timezone.utc)
        return (
            self.db.query(RecurringPayment)
            .filter(
                RecurringPayment.is_active.is_(True),
                RecurringPayment.next_payment_at <= now,
            )
            .all()
        )

    def get_due_payments_for_update(self) -> List[RecurringPayment]:
        """Return due active payments with a row-level lock (SKIP LOCKED on Postgres).

        On PostgreSQL this compiles to ``SELECT ... FOR UPDATE SKIP LOCKED`` which
        prevents two cron replicas from picking up the same rows simultaneously.
        On SQLite (used in tests) the lock clause is silently dropped — correctness
        is guaranteed there by the distributed lease layer instead.
        """
        now = datetime.now(timezone.utc)
        dialect = getattr(self.db.bind, "dialect", None)
        dialect_name = dialect.name if dialect else ""

        query = self.db.query(RecurringPayment).filter(
            RecurringPayment.is_active.is_(True),
            RecurringPayment.next_payment_at <= now,
        )

        # Only apply SKIP LOCKED on PostgreSQL; SQLite does not support it.
        if dialect_name == "postgresql":
            query = query.with_for_update(skip_locked=True)

        return query.all()

    def get_by_id_for_update(self, payment_id: UUID) -> Optional[RecurringPayment]:
        """Fetch a payment by ID with a row-level lock (FOR UPDATE).

        Used during the charge window so that concurrent ``cancel_subscription``
        calls must wait for the charge transaction to commit or roll back.
        On SQLite the lock is silently dropped (single-process test environment).
        """
        dialect = getattr(self.db.bind, "dialect", None)
        dialect_name = dialect.name if dialect else ""

        query = self.db.query(RecurringPayment).filter(
            RecurringPayment.id == payment_id
        )

        if dialect_name == "postgresql":
            query = query.with_for_update()

        return query.first()

    def list_by_agent(
        self,
        agent_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[RecurringPayment], int]:
        """List payments where agent_id is sender or receiver. Returns (items, total)."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(RecurringPayment).filter(
            or_(
                RecurringPayment.from_agent_id == agent_id,
                RecurringPayment.to_agent_id == agent_id,
            )
        )
        total = query.count()
        items = (
            query.order_by(desc(RecurringPayment.created_at))
            .offset(offset)
            .limit(limit)
            .all()
        )
        return items, total

    def record_payment(
        self,
        payment_id: UUID,
        next_payment_at: datetime,
    ) -> int:
        """Increment payments_made, add amount to total_paid, update timestamps.

        Returns the number of rows affected (0 if the payment no longer exists).
        """
        now = datetime.now(timezone.utc)
        # We need the current amount to add to total_paid, so fetch first.
        payment = self.get_by_id(payment_id)
        if payment is None:
            return 0
        amount = payment.amount or Decimal("0")
        return (
            self.db.query(RecurringPayment)
            .filter(RecurringPayment.id == payment_id)
            .update(
                {
                    "payments_made": RecurringPayment.payments_made + 1,
                    "total_paid": RecurringPayment.total_paid + amount,
                    "last_payment_at": now,
                    "next_payment_at": next_payment_at,
                }
            )
        )

    def cancel(self, payment_id: UUID) -> int:
        """Deactivate the payment. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return (
            self.db.query(RecurringPayment)
            .filter(RecurringPayment.id == payment_id)
            .update(
                {
                    "is_active": False,
                    "cancelled_at": now,
                }
            )
        )

    def update(
        self,
        payment_id: UUID,
        amount: Optional[Decimal] = None,
        interval: Optional[RecurringInterval] = None,
    ) -> int:
        """Update mutable fields. Returns rows affected (0 if nothing changed)."""
        updates = {}
        if amount is not None:
            updates["amount"] = amount
        if interval is not None:
            updates["interval"] = interval
        if not updates:
            return 0
        return (
            self.db.query(RecurringPayment)
            .filter(RecurringPayment.id == payment_id)
            .update(updates)
        )
