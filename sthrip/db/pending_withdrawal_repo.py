"""
PendingWithdrawalRepository — saga journal for withdrawal operations.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, List
from uuid import UUID

from sqlalchemy.orm import Session

from .enums import WithdrawalStatus
from .models import PendingWithdrawal


class PendingWithdrawalRepository:
    """Saga journal for withdrawal operations."""

    def __init__(self, db: Session):
        self.db = db

    def create(self, agent_id: UUID, amount: Decimal, address: str) -> PendingWithdrawal:
        pw = PendingWithdrawal(
            agent_id=agent_id,
            amount=amount,
            address=address,
            status=WithdrawalStatus.PENDING,
        )
        self.db.add(pw)
        self.db.flush()
        return pw

    def get_by_id(self, pw_id: str) -> Optional[PendingWithdrawal]:
        return self.db.query(PendingWithdrawal).filter_by(id=pw_id).first()

    def get_stale_pending(self, max_age_minutes: int = 5) -> List[PendingWithdrawal]:
        """Get pending withdrawals older than max_age_minutes."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        return self.db.query(PendingWithdrawal).filter(
            PendingWithdrawal.status == WithdrawalStatus.PENDING,
            PendingWithdrawal.created_at < cutoff,
        ).all()

    def get_pending(self) -> List[PendingWithdrawal]:
        return self.db.query(PendingWithdrawal).filter_by(status=WithdrawalStatus.PENDING).all()

    def mark_completed(self, pw_id: str, tx_hash: str) -> None:
        self.db.query(PendingWithdrawal).filter_by(id=pw_id).update({
            "status": WithdrawalStatus.COMPLETED,
            "tx_hash": tx_hash,
            "completed_at": datetime.now(timezone.utc),
        })
        self.db.flush()

    def mark_failed(self, pw_id: str, error: str) -> None:
        self.db.query(PendingWithdrawal).filter_by(id=pw_id).update({
            "status": WithdrawalStatus.FAILED,
            "error": error,
            "completed_at": datetime.now(timezone.utc),
        })
        self.db.flush()

    def mark_needs_review(self, pw_id: str, reason: str) -> None:
        self.db.query(PendingWithdrawal).filter_by(id=pw_id).update({
            "status": WithdrawalStatus.NEEDS_REVIEW,
            "error": reason,
        })
        self.db.flush()
