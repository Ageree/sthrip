"""
MultiPartyRepository -- data-access layer for MultiPartyPayment and
MultiPartyRecipient records.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import desc, or_

from .models import (
    MultiPartyPayment, MultiPartyRecipient, MultiPartyPaymentState,
)
from ._repo_base import _MAX_QUERY_LIMIT


class MultiPartyRepository:
    """Multi-party payment data access."""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        payment_hash: str,
        sender_id: UUID,
        total_amount: Decimal,
        recipients: List[dict],
        accept_deadline: datetime,
        currency: str = "XMR",
        require_all_accept: bool = True,
    ) -> MultiPartyPayment:
        """Create a multi-party payment with recipients.

        Args:
            recipients: list of {"recipient_id": UUID, "amount": Decimal}
        """
        payment = MultiPartyPayment(
            payment_hash=payment_hash,
            sender_id=sender_id,
            total_amount=total_amount,
            currency=currency,
            require_all_accept=require_all_accept,
            state=MultiPartyPaymentState.PENDING,
            accept_deadline=accept_deadline,
        )
        self.db.add(payment)
        self.db.flush()

        for r in recipients:
            recipient = MultiPartyRecipient(
                payment_id=payment.id,
                recipient_id=r["recipient_id"],
                amount=r["amount"],
            )
            self.db.add(recipient)

        self.db.flush()
        return payment

    def get_by_id(self, payment_id: UUID) -> Optional[MultiPartyPayment]:
        """Get multi-party payment by ID (eager-loads recipients)."""
        return self.db.query(MultiPartyPayment).filter(
            MultiPartyPayment.id == payment_id,
        ).first()

    def get_by_id_for_update(self, payment_id: UUID) -> Optional[MultiPartyPayment]:
        """Get multi-party payment by ID with row-level lock."""
        is_sqlite = self.db.bind and self.db.bind.dialect.name == "sqlite"
        query = self.db.query(MultiPartyPayment).filter(
            MultiPartyPayment.id == payment_id,
        )
        if not is_sqlite:
            query = query.with_for_update()
        return query.first()

    def accept_recipient(self, payment_id: UUID, recipient_id: UUID) -> int:
        """Mark a recipient as accepted. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(MultiPartyRecipient).filter(
            MultiPartyRecipient.payment_id == payment_id,
            MultiPartyRecipient.recipient_id == recipient_id,
            MultiPartyRecipient.accepted.is_(None),
        ).update({
            "accepted": True,
            "accepted_at": now,
        })

    def reject_recipient(self, payment_id: UUID, recipient_id: UUID) -> int:
        """Mark a recipient as rejected. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(MultiPartyRecipient).filter(
            MultiPartyRecipient.payment_id == payment_id,
            MultiPartyRecipient.recipient_id == recipient_id,
            MultiPartyRecipient.accepted.is_(None),
        ).update({
            "accepted": False,
            "accepted_at": now,
        })

    def complete(self, payment_id: UUID) -> int:
        """Transition PENDING -> COMPLETED. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(MultiPartyPayment).filter(
            MultiPartyPayment.id == payment_id,
            MultiPartyPayment.state == MultiPartyPaymentState.PENDING,
        ).update({
            "state": MultiPartyPaymentState.COMPLETED,
            "completed_at": now,
        })

    def reject(self, payment_id: UUID) -> int:
        """Transition PENDING -> REJECTED. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(MultiPartyPayment).filter(
            MultiPartyPayment.id == payment_id,
            MultiPartyPayment.state == MultiPartyPaymentState.PENDING,
        ).update({
            "state": MultiPartyPaymentState.REJECTED,
            "completed_at": now,
        })

    def expire(self, payment_id: UUID) -> int:
        """Transition PENDING -> EXPIRED. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(MultiPartyPayment).filter(
            MultiPartyPayment.id == payment_id,
            MultiPartyPayment.state == MultiPartyPaymentState.PENDING,
        ).update({
            "state": MultiPartyPaymentState.EXPIRED,
            "completed_at": now,
        })

    def get_recipients(self, payment_id: UUID) -> List[MultiPartyRecipient]:
        """Get all recipients for a payment."""
        return self.db.query(MultiPartyRecipient).filter(
            MultiPartyRecipient.payment_id == payment_id,
        ).all()

    def list_by_agent(
        self,
        agent_id: UUID,
        role: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[MultiPartyPayment], int]:
        """List multi-party payments for an agent. Returns (items, total)."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(MultiPartyPayment)

        if role == "sender":
            query = query.filter(MultiPartyPayment.sender_id == agent_id)
        elif role == "recipient":
            # Subquery to find payments where agent is a recipient
            recipient_payment_ids = self.db.query(
                MultiPartyRecipient.payment_id
            ).filter(
                MultiPartyRecipient.recipient_id == agent_id,
            ).subquery()
            query = query.filter(MultiPartyPayment.id.in_(
                self.db.query(recipient_payment_ids)
            ))
        else:
            recipient_payment_ids = self.db.query(
                MultiPartyRecipient.payment_id
            ).filter(
                MultiPartyRecipient.recipient_id == agent_id,
            ).subquery()
            query = query.filter(
                or_(
                    MultiPartyPayment.sender_id == agent_id,
                    MultiPartyPayment.id.in_(
                        self.db.query(recipient_payment_ids)
                    ),
                )
            )

        if state:
            query = query.filter(MultiPartyPayment.state == state)

        total = query.count()
        items = (
            query.order_by(desc(MultiPartyPayment.created_at))
            .offset(offset)
            .limit(limit)
            .all()
        )
        return items, total

    def get_pending_expired(self) -> List[MultiPartyPayment]:
        """Get pending payments past their accept deadline (for auto-expiry)."""
        now = datetime.now(timezone.utc)
        return self.db.query(MultiPartyPayment).filter(
            MultiPartyPayment.state == MultiPartyPaymentState.PENDING,
            MultiPartyPayment.accept_deadline <= now,
        ).all()
