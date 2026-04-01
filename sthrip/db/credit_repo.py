"""
CreditRepository -- data-access layer for AgentCreditScore records.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from .models import AgentCreditScore


class CreditRepository:
    """Credit score data access."""

    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self, agent_id: UUID) -> AgentCreditScore:
        """Get credit score record, create with defaults if not exists."""
        record = self.db.query(AgentCreditScore).filter(
            AgentCreditScore.agent_id == agent_id,
        ).first()
        if record is not None:
            return record

        record = AgentCreditScore(agent_id=agent_id)
        self.db.add(record)
        self.db.flush()
        return record

    def get(self, agent_id: UUID) -> Optional[AgentCreditScore]:
        """Get credit score record (None if not exists)."""
        return self.db.query(AgentCreditScore).filter(
            AgentCreditScore.agent_id == agent_id,
        ).first()

    def update_score(
        self,
        agent_id: UUID,
        credit_score: int,
        max_borrow_amount: Decimal,
        max_concurrent_loans: int,
    ) -> int:
        """Update computed credit score and derived limits. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(AgentCreditScore).filter(
            AgentCreditScore.agent_id == agent_id,
        ).update({
            "credit_score": credit_score,
            "max_borrow_amount": max_borrow_amount,
            "max_concurrent_loans": max_concurrent_loans,
            "calculated_at": now,
        })

    def record_loan_taken(self, agent_id: UUID, principal: Decimal) -> None:
        """Increment loan-taken counters after a loan is funded."""
        record = self.get_or_create(agent_id)
        record.total_loans_taken = (record.total_loans_taken or 0) + 1
        record.total_borrowed_volume = (record.total_borrowed_volume or Decimal("0")) + principal
        self.db.flush()

    def record_loan_repaid(self, agent_id: UUID, repayment_time_secs: int) -> None:
        """Increment repayment counter and update average repayment time."""
        record = self.get_or_create(agent_id)
        record.total_loans_repaid = (record.total_loans_repaid or 0) + 1
        # Rolling average of repayment time
        prev_avg = record.avg_repayment_time_secs or 0
        prev_count = record.total_loans_repaid - 1  # count before this repayment
        if prev_count <= 0:
            record.avg_repayment_time_secs = repayment_time_secs
        else:
            record.avg_repayment_time_secs = int(
                (prev_avg * prev_count + repayment_time_secs) / record.total_loans_repaid
            )
        self.db.flush()

    def record_loan_defaulted(self, agent_id: UUID, default_duration_secs: int) -> None:
        """Increment default counter and track worst default."""
        record = self.get_or_create(agent_id)
        record.total_loans_defaulted = (record.total_loans_defaulted or 0) + 1
        current_worst = record.longest_default_secs or 0
        if default_duration_secs > current_worst:
            record.longest_default_secs = default_duration_secs
        self.db.flush()
