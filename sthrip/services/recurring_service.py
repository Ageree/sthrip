"""
RecurringService — business logic for server-side recurring payment schedules.

Flow:
  - Subscriber calls create_subscription() to set up a schedule.
  - Background task calls execute_due_payments() every 5 minutes.
  - Either participant can cancel; only the sender can update.

Fee: 1% of each payment, credited to the hub FeeCollection ledger via
BalanceRepository.credit(hub_fee_agent).  Net amount credited to receiver =
amount * 0.99.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.models import RecurringInterval
from sthrip.db.recurring_repo import RecurringPaymentRepository
from sthrip.db.balance_repo import BalanceRepository
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.webhook_service import queue_webhook

logger = logging.getLogger("sthrip.recurring")

_FEE_RATE = Decimal("0.01")  # 1%


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _payment_to_dict(payment) -> dict:
    interval_val = (
        payment.interval.value
        if hasattr(payment.interval, "value")
        else payment.interval
    )
    return {
        "id": str(payment.id),
        "from_agent_id": str(payment.from_agent_id),
        "to_agent_id": str(payment.to_agent_id),
        "amount": str(payment.amount),
        "interval": interval_val,
        "next_payment_at": _iso(payment.next_payment_at),
        "last_payment_at": _iso(payment.last_payment_at),
        "total_paid": str(payment.total_paid or Decimal("0")),
        "max_payments": payment.max_payments,
        "payments_made": payment.payments_made or 0,
        "is_active": payment.is_active,
        "created_at": _iso(payment.created_at),
        "cancelled_at": _iso(payment.cancelled_at),
    }


class RecurringService:
    """Service layer for recurring payment schedules."""

    def create_subscription(
        self,
        db: Session,
        from_agent_id: UUID,
        to_agent_id: UUID,
        amount: Decimal,
        interval: RecurringInterval,
        max_payments: Optional[int] = None,
    ) -> dict:
        """Create a new recurring subscription.

        Validates that:
          - from_agent != to_agent
          - amount > 0

        Calculates the first next_payment_at, persists the record, fires
        audit_log and queue_webhook, then returns an immutable dict.
        """
        if from_agent_id == to_agent_id:
            raise ValueError("Agent cannot subscribe to yourself")

        if amount <= Decimal("0"):
            raise ValueError("Subscription amount must be positive")

        next_payment_at = self._calculate_next_payment(interval, _now())

        repo = RecurringPaymentRepository(db)
        payment = repo.create(
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            amount=amount,
            interval=interval,
            max_payments=max_payments,
            next_payment_at=next_payment_at,
        )
        db.flush()

        result = _payment_to_dict(payment)

        audit_log(
            "recurring.created",
            agent_id=from_agent_id,
            details={
                "subscription_id": result["id"],
                "to_agent_id": result["to_agent_id"],
                "amount": result["amount"],
                "interval": result["interval"],
            },
        )
        queue_webhook(
            db=db,
            agent_id=from_agent_id,
            event_type="recurring.created",
            payload=result,
        )

        return result

    def execute_due_payments(self, db: Session) -> int:
        """Execute all due recurring payments.

        For each due payment:
          1. Check sender has sufficient balance.
          2. Deduct from sender.
          3. Calculate 1% fee; credit (amount - fee) to receiver.
          4. Advance next_payment_at.
          5. If max_payments reached, cancel the subscription.

        Returns count of successfully executed payments.
        """
        repo = RecurringPaymentRepository(db)
        bal_repo = BalanceRepository(db)
        due_payments = repo.get_due_payments()

        executed = 0
        for payment in due_payments:
            amount = payment.amount or Decimal("0")
            from_id = payment.from_agent_id
            to_id = payment.to_agent_id

            available = bal_repo.get_available(from_id)
            if available < amount:
                logger.warning(
                    "Recurring payment %s skipped: insufficient balance "
                    "(available=%s, required=%s)",
                    payment.id,
                    available,
                    amount,
                )
                continue

            fee = (amount * _FEE_RATE).quantize(Decimal("0.00000001"))
            net_to_receiver = amount - fee

            # Deduct from sender
            bal_repo.deduct(from_id, amount)

            # Credit receiver (net of fee)
            bal_repo.credit(to_id, net_to_receiver)

            # Advance schedule
            next_at = self._calculate_next_payment(payment.interval, _now())
            repo.record_payment(payment.id, next_at)

            # Cancel if max_payments reached
            new_payments_made = (payment.payments_made or 0) + 1
            if payment.max_payments is not None and new_payments_made >= payment.max_payments:
                repo.cancel(payment.id)
                logger.info(
                    "Recurring payment %s completed max_payments=%d; cancelled.",
                    payment.id,
                    payment.max_payments,
                )

            executed += 1

        return executed

    def cancel_subscription(
        self,
        db: Session,
        payment_id: UUID,
        agent_id: UUID,
    ) -> dict:
        """Cancel a subscription. Either participant may cancel.

        Raises:
            LookupError: subscription not found.
            PermissionError: caller is not from_agent or to_agent.
        """
        repo = RecurringPaymentRepository(db)
        payment = repo.get_by_id(payment_id)
        if payment is None:
            raise LookupError(f"Subscription {payment_id} not found")

        if agent_id not in (payment.from_agent_id, payment.to_agent_id):
            raise PermissionError("Only a participant may cancel this subscription")

        repo.cancel(payment_id)
        db.flush()

        refreshed = repo.get_by_id(payment_id)
        result = _payment_to_dict(refreshed)

        audit_log(
            "recurring.cancelled",
            agent_id=agent_id,
            details={"subscription_id": str(payment_id)},
        )
        queue_webhook(
            db=db,
            agent_id=agent_id,
            event_type="recurring.cancelled",
            payload=result,
        )

        return result

    def update_subscription(
        self,
        db: Session,
        payment_id: UUID,
        agent_id: UUID,
        amount: Optional[Decimal] = None,
        interval: Optional[RecurringInterval] = None,
    ) -> dict:
        """Update a subscription. Only the sender (from_agent) may update.

        Raises:
            LookupError: subscription not found.
            PermissionError: caller is not from_agent.
        """
        repo = RecurringPaymentRepository(db)
        payment = repo.get_by_id(payment_id)
        if payment is None:
            raise LookupError(f"Subscription {payment_id} not found")

        if agent_id != payment.from_agent_id:
            raise PermissionError("Only the sender may update this subscription")

        repo.update(payment_id, amount=amount, interval=interval)
        db.flush()

        refreshed = repo.get_by_id(payment_id)
        result = _payment_to_dict(refreshed)

        audit_log(
            "recurring.updated",
            agent_id=agent_id,
            details={"subscription_id": str(payment_id)},
        )
        queue_webhook(
            db=db,
            agent_id=agent_id,
            event_type="recurring.updated",
            payload=result,
        )

        return result

    @staticmethod
    def _calculate_next_payment(
        interval: RecurringInterval,
        from_time: datetime,
    ) -> datetime:
        """Calculate the next payment datetime from the given base time."""
        if interval == RecurringInterval.HOURLY:
            return from_time + timedelta(hours=1)
        if interval == RecurringInterval.DAILY:
            return from_time + timedelta(days=1)
        if interval == RecurringInterval.WEEKLY:
            return from_time + timedelta(days=7)
        if interval == RecurringInterval.MONTHLY:
            return from_time + timedelta(days=30)
        raise ValueError(f"Unknown interval: {interval}")
