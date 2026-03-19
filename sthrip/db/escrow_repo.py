"""
EscrowRepository — data-access layer for hub-held EscrowDeal records.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, List, Tuple
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import desc, or_

from . import models
from .models import EscrowStatus
from ._repo_base import _MAX_QUERY_LIMIT


class EscrowRepository:
    """Hub-held escrow deal data access"""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        deal_hash: str,
        buyer_id: UUID,
        seller_id: UUID,
        amount: Decimal,
        description: str,
        accept_timeout_hours: int = 24,
        delivery_timeout_hours: int = 48,
        review_timeout_hours: int = 24,
        fee_percent: Decimal = Decimal("0.001"),
    ) -> models.EscrowDeal:
        """Create new hub-held escrow deal in CREATED state."""
        now = datetime.now(timezone.utc)
        accept_deadline = now + timedelta(hours=accept_timeout_hours)

        deal = models.EscrowDeal(
            deal_hash=deal_hash,
            buyer_id=buyer_id,
            seller_id=seller_id,
            amount=amount,
            description=description,
            accept_timeout_hours=accept_timeout_hours,
            delivery_timeout_hours=delivery_timeout_hours,
            review_timeout_hours=review_timeout_hours,
            fee_percent=fee_percent,
            status=EscrowStatus.CREATED,
            accept_deadline=accept_deadline,
            expires_at=accept_deadline,
        )

        self.db.add(deal)
        self.db.flush()
        return deal

    def get_by_id(self, deal_id: UUID) -> Optional[models.EscrowDeal]:
        """Get deal by ID."""
        return self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).first()

    def get_by_id_for_update(self, deal_id: UUID) -> Optional[models.EscrowDeal]:
        """Get deal by ID with row-level lock."""
        is_sqlite = self.db.bind and self.db.bind.dialect.name == "sqlite"
        query = self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        )
        if not is_sqlite:
            query = query.with_for_update()
        return query.first()

    def get_by_hash(self, deal_hash: str) -> Optional[models.EscrowDeal]:
        """Get deal by hash."""
        return self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.deal_hash == deal_hash
        ).first()

    def list_by_agent(
        self,
        agent_id: UUID,
        role: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[models.EscrowDeal], int]:
        """List deals where agent participates. Returns (items, total)."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(models.EscrowDeal)

        if role == "buyer":
            query = query.filter(models.EscrowDeal.buyer_id == agent_id)
        elif role == "seller":
            query = query.filter(models.EscrowDeal.seller_id == agent_id)
        else:
            query = query.filter(
                or_(
                    models.EscrowDeal.buyer_id == agent_id,
                    models.EscrowDeal.seller_id == agent_id,
                )
            )

        if status:
            query = query.filter(models.EscrowDeal.status == status)

        total = query.count()
        items = (
            query.order_by(desc(models.EscrowDeal.created_at))
            .offset(offset)
            .limit(limit)
            .all()
        )
        return items, total

    def accept(self, deal_id: UUID, delivery_timeout_hours: int) -> int:
        """Transition CREATED → ACCEPTED, set delivery deadline.

        Caller must hold the row lock via get_by_id_for_update.
        Returns rows affected (0 if state already changed).
        """
        now = datetime.now(timezone.utc)
        delivery_deadline = now + timedelta(hours=delivery_timeout_hours)
        return self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id,
            models.EscrowDeal.status == EscrowStatus.CREATED,
        ).update({
            "status": EscrowStatus.ACCEPTED,
            "accepted_at": now,
            "delivery_deadline": delivery_deadline,
            "expires_at": delivery_deadline,
        })

    def deliver(self, deal_id: UUID, review_timeout_hours: int) -> int:
        """Transition ACCEPTED → DELIVERED, set review deadline.

        Caller must hold the row lock via get_by_id_for_update.
        Returns rows affected (0 if state already changed).
        """
        now = datetime.now(timezone.utc)
        review_deadline = now + timedelta(hours=review_timeout_hours)
        return self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id,
            models.EscrowDeal.status == EscrowStatus.ACCEPTED,
        ).update({
            "status": EscrowStatus.DELIVERED,
            "delivered_at": now,
            "review_deadline": review_deadline,
            "expires_at": review_deadline,
        })

    def release(
        self,
        deal_id: UUID,
        release_amount: Decimal,
        fee_amount: Decimal,
    ) -> int:
        """Transition DELIVERED → COMPLETED with release amount and fee.

        Returns number of rows affected (0 if already transitioned).
        """
        now = datetime.now(timezone.utc)
        return self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id,
            models.EscrowDeal.status == EscrowStatus.DELIVERED,
        ).update({
            "status": EscrowStatus.COMPLETED,
            "release_amount": release_amount,
            "fee_amount": fee_amount,
            "completed_at": now,
        })

    def cancel(self, deal_id: UUID) -> int:
        """Transition CREATED → CANCELLED. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id,
            models.EscrowDeal.status == EscrowStatus.CREATED,
        ).update({
            "status": EscrowStatus.CANCELLED,
            "cancelled_at": now,
        })

    def expire(self, deal_id: UUID) -> int:
        """Transition CREATED/ACCEPTED → EXPIRED. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id,
            models.EscrowDeal.status.in_([
                EscrowStatus.CREATED,
                EscrowStatus.ACCEPTED,
            ]),
        ).update({
            "status": EscrowStatus.EXPIRED,
            "completed_at": now,
        })

    def get_pending_expiry(self) -> List[models.EscrowDeal]:
        """Get escrows that have passed their deadline and need auto-resolution."""
        now = datetime.now(timezone.utc)
        return self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.status.in_([
                EscrowStatus.CREATED,
                EscrowStatus.ACCEPTED,
                EscrowStatus.DELIVERED,
            ]),
            models.EscrowDeal.expires_at <= now,
        ).all()
