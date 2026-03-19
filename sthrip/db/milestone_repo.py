"""
MilestoneRepository — data-access layer for EscrowMilestone records.

Follows the same patterns as EscrowRepository:
  - Status guards in WHERE clauses to prevent invalid transitions
  - Row-level locking via with_for_update() (skipped on SQLite)
  - Mutations return rowcount so callers detect concurrent state changes
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from . import models
from .models import MilestoneStatus


class MilestoneRepository:
    """Data access for escrow milestones."""

    def __init__(self, db: Session):
        self.db = db

    def create_milestones(
        self,
        escrow_id: UUID,
        milestones_data: list,
        fee_percent: Decimal,
    ) -> List[models.EscrowMilestone]:
        """Create milestone records for a multi-milestone escrow.

        Args:
            escrow_id: Parent escrow deal ID.
            milestones_data: List of dicts with keys: description, amount,
                delivery_timeout_hours, review_timeout_hours.
            fee_percent: Fee percentage from the parent deal (stored for
                reference but not used directly on the milestone model).

        Returns:
            List of created EscrowMilestone ORM objects in sequence order.
        """
        created: List[models.EscrowMilestone] = []
        for idx, m in enumerate(milestones_data, start=1):
            milestone = models.EscrowMilestone(
                escrow_id=escrow_id,
                sequence=idx,
                description=m["description"],
                amount=m["amount"],
                delivery_timeout_hours=m["delivery_timeout_hours"],
                review_timeout_hours=m["review_timeout_hours"],
                status=MilestoneStatus.PENDING,
            )
            self.db.add(milestone)
            created.append(milestone)
        self.db.flush()
        return created

    # ── Read queries ─────────────────────────────────────────────────────

    def get_by_escrow_and_sequence(
        self, escrow_id: UUID, sequence: int,
    ) -> Optional[models.EscrowMilestone]:
        """Get a milestone by escrow ID and sequence number."""
        return self.db.query(models.EscrowMilestone).filter(
            models.EscrowMilestone.escrow_id == escrow_id,
            models.EscrowMilestone.sequence == sequence,
        ).first()

    def get_by_escrow_and_sequence_for_update(
        self, escrow_id: UUID, sequence: int,
    ) -> Optional[models.EscrowMilestone]:
        """Get a milestone with row-level lock for safe mutations."""
        is_sqlite = self.db.bind and self.db.bind.dialect.name == "sqlite"
        query = self.db.query(models.EscrowMilestone).filter(
            models.EscrowMilestone.escrow_id == escrow_id,
            models.EscrowMilestone.sequence == sequence,
        )
        if not is_sqlite:
            query = query.with_for_update()
        return query.first()

    def get_by_escrow(self, escrow_id: UUID) -> List[models.EscrowMilestone]:
        """Get all milestones for an escrow, ordered by sequence."""
        return (
            self.db.query(models.EscrowMilestone)
            .filter(models.EscrowMilestone.escrow_id == escrow_id)
            .order_by(models.EscrowMilestone.sequence)
            .all()
        )

    # ── State transitions ────────────────────────────────────────────────

    def activate(self, milestone_id: UUID, delivery_timeout_hours: int) -> int:
        """Transition PENDING -> ACTIVE, set delivery deadline.

        Returns rows affected (0 if status guard prevented the update).
        """
        now = datetime.now(timezone.utc)
        delivery_deadline = now + timedelta(hours=delivery_timeout_hours)
        return self.db.query(models.EscrowMilestone).filter(
            models.EscrowMilestone.id == milestone_id,
            models.EscrowMilestone.status == MilestoneStatus.PENDING,
        ).update({
            "status": MilestoneStatus.ACTIVE,
            "activated_at": now,
            "delivery_deadline": delivery_deadline,
            "expires_at": delivery_deadline,
        })

    def deliver(self, milestone_id: UUID, review_timeout_hours: int) -> int:
        """Transition ACTIVE -> DELIVERED, set review deadline.

        Returns rows affected (0 if status guard prevented the update).
        """
        now = datetime.now(timezone.utc)
        review_deadline = now + timedelta(hours=review_timeout_hours)
        return self.db.query(models.EscrowMilestone).filter(
            models.EscrowMilestone.id == milestone_id,
            models.EscrowMilestone.status == MilestoneStatus.ACTIVE,
        ).update({
            "status": MilestoneStatus.DELIVERED,
            "delivered_at": now,
            "review_deadline": review_deadline,
            "expires_at": review_deadline,
        })

    def release(
        self,
        milestone_id: UUID,
        release_amount: Decimal,
        fee_amount: Decimal,
    ) -> int:
        """Transition DELIVERED -> COMPLETED with release and fee amounts.

        Returns rows affected (0 if status guard prevented the update).
        """
        now = datetime.now(timezone.utc)
        return self.db.query(models.EscrowMilestone).filter(
            models.EscrowMilestone.id == milestone_id,
            models.EscrowMilestone.status == MilestoneStatus.DELIVERED,
        ).update({
            "status": MilestoneStatus.COMPLETED,
            "release_amount": release_amount,
            "fee_amount": fee_amount,
            "completed_at": now,
        })

    def expire(self, milestone_id: UUID) -> int:
        """Transition ACTIVE -> EXPIRED.

        Returns rows affected (0 if status guard prevented the update).
        """
        now = datetime.now(timezone.utc)
        return self.db.query(models.EscrowMilestone).filter(
            models.EscrowMilestone.id == milestone_id,
            models.EscrowMilestone.status == MilestoneStatus.ACTIVE,
        ).update({
            "status": MilestoneStatus.EXPIRED,
            "expires_at": now,
        })

    def cancel_pending(self, escrow_id: UUID) -> int:
        """Cancel all PENDING milestones for a given escrow.

        Returns total rows affected.
        """
        now = datetime.now(timezone.utc)
        return self.db.query(models.EscrowMilestone).filter(
            models.EscrowMilestone.escrow_id == escrow_id,
            models.EscrowMilestone.status == MilestoneStatus.PENDING,
        ).update({
            "status": MilestoneStatus.CANCELLED,
            "cancelled_at": now,
        })

    def get_pending_milestone_expiry(self) -> List[models.EscrowMilestone]:
        """Get milestones that have passed their deadline and need auto-resolution.

        Returns ACTIVE milestones past delivery_deadline and DELIVERED milestones
        past review_deadline.
        """
        now = datetime.now(timezone.utc)
        return self.db.query(models.EscrowMilestone).filter(
            models.EscrowMilestone.expires_at <= now,
            models.EscrowMilestone.status.in_([
                MilestoneStatus.ACTIVE,
                MilestoneStatus.DELIVERED,
            ]),
        ).all()
