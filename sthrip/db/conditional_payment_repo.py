"""
ConditionalPaymentRepository -- data-access layer for ConditionalPayment records.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import desc, or_

from .models import ConditionalPayment, ConditionalPaymentState
from ._repo_base import _MAX_QUERY_LIMIT


class ConditionalPaymentRepository:
    """Conditional payment data access."""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        payment_hash: str,
        from_agent_id: UUID,
        to_agent_id: UUID,
        amount: Decimal,
        condition_type: str,
        condition_config: Dict[str, Any],
        locked_amount: Decimal,
        expires_at: datetime,
        currency: str = "XMR",
        memo: Optional[str] = None,
    ) -> ConditionalPayment:
        """Create a new conditional payment in PENDING state."""
        payment = ConditionalPayment(
            payment_hash=payment_hash,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            amount=amount,
            currency=currency,
            memo=memo,
            condition_type=condition_type,
            condition_config=condition_config,
            locked_amount=locked_amount,
            state=ConditionalPaymentState.PENDING,
            expires_at=expires_at,
        )
        self.db.add(payment)
        self.db.flush()
        return payment

    def get_by_id(self, payment_id: UUID) -> Optional[ConditionalPayment]:
        """Get conditional payment by ID."""
        return self.db.query(ConditionalPayment).filter(
            ConditionalPayment.id == payment_id,
        ).first()

    def get_by_id_for_update(self, payment_id: UUID) -> Optional[ConditionalPayment]:
        """Get conditional payment by ID with row-level lock."""
        is_sqlite = self.db.bind and self.db.bind.dialect.name == "sqlite"
        query = self.db.query(ConditionalPayment).filter(
            ConditionalPayment.id == payment_id,
        )
        if not is_sqlite:
            query = query.with_for_update()
        return query.first()

    def trigger(self, payment_id: UUID) -> int:
        """Transition PENDING -> TRIGGERED. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(ConditionalPayment).filter(
            ConditionalPayment.id == payment_id,
            ConditionalPayment.state == ConditionalPaymentState.PENDING,
        ).update({
            "state": ConditionalPaymentState.TRIGGERED,
            "triggered_at": now,
        })

    def execute(self, payment_id: UUID) -> int:
        """Transition TRIGGERED -> EXECUTED. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(ConditionalPayment).filter(
            ConditionalPayment.id == payment_id,
            ConditionalPayment.state == ConditionalPaymentState.TRIGGERED,
        ).update({
            "state": ConditionalPaymentState.EXECUTED,
            "executed_at": now,
        })

    def expire(self, payment_id: UUID) -> int:
        """Transition PENDING -> EXPIRED. Returns rows affected."""
        return self.db.query(ConditionalPayment).filter(
            ConditionalPayment.id == payment_id,
            ConditionalPayment.state == ConditionalPaymentState.PENDING,
        ).update({
            "state": ConditionalPaymentState.EXPIRED,
        })

    def cancel(self, payment_id: UUID) -> int:
        """Transition PENDING -> CANCELLED. Returns rows affected."""
        return self.db.query(ConditionalPayment).filter(
            ConditionalPayment.id == payment_id,
            ConditionalPayment.state == ConditionalPaymentState.PENDING,
        ).update({
            "state": ConditionalPaymentState.CANCELLED,
        })

    def list_by_agent(
        self,
        agent_id: UUID,
        role: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[ConditionalPayment], int]:
        """List conditional payments for an agent. Returns (items, total)."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(ConditionalPayment)

        if role == "sender":
            query = query.filter(ConditionalPayment.from_agent_id == agent_id)
        elif role == "recipient":
            query = query.filter(ConditionalPayment.to_agent_id == agent_id)
        else:
            query = query.filter(
                or_(
                    ConditionalPayment.from_agent_id == agent_id,
                    ConditionalPayment.to_agent_id == agent_id,
                )
            )

        if state:
            query = query.filter(ConditionalPayment.state == state)

        total = query.count()
        items = (
            query.order_by(desc(ConditionalPayment.created_at))
            .offset(offset)
            .limit(limit)
            .all()
        )
        return items, total

    def get_pending_expired(self) -> List[ConditionalPayment]:
        """Get pending payments past their expiry (for auto-expiry task)."""
        now = datetime.now(timezone.utc)
        return self.db.query(ConditionalPayment).filter(
            ConditionalPayment.state == ConditionalPaymentState.PENDING,
            ConditionalPayment.expires_at <= now,
        ).all()
