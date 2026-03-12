"""
ReputationRepository — data-access layer for AgentReputation records.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List
from uuid import UUID

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc, update

from . import models


class ReputationRepository:
    """Agent reputation data access"""

    def __init__(self, db: Session):
        self.db = db

    def get_by_agent(self, agent_id: UUID) -> Optional[models.AgentReputation]:
        """Get reputation for agent"""
        return self.db.query(models.AgentReputation).filter(
            models.AgentReputation.agent_id == agent_id
        ).first()

    def record_transaction(
        self,
        agent_id: UUID,
        success: bool = True,
        amount_usd: Decimal = Decimal('0')
    ):
        """Record transaction for reputation using atomic SQL updates."""
        values = {
            models.AgentReputation.total_transactions: models.AgentReputation.total_transactions + 1,
            models.AgentReputation.total_volume_usd: models.AgentReputation.total_volume_usd + amount_usd,
            models.AgentReputation.calculated_at: datetime.now(timezone.utc),
        }
        if success:
            values[models.AgentReputation.successful_transactions] = (
                models.AgentReputation.successful_transactions + 1
            )
        else:
            values[models.AgentReputation.failed_transactions] = (
                models.AgentReputation.failed_transactions + 1
            )

        self.db.execute(
            update(models.AgentReputation)
            .where(models.AgentReputation.agent_id == agent_id)
            .values(**values)
        )

    def record_dispute(self, agent_id: UUID):
        """Record dispute for agent — atomic SQL increment."""
        self.db.execute(
            update(models.AgentReputation)
            .where(models.AgentReputation.agent_id == agent_id)
            .values(
                disputed_transactions=models.AgentReputation.disputed_transactions + 1
            )
        )

    def get_leaderboard(self, limit: int = 100) -> List[models.AgentReputation]:
        """Get top agents by trust score"""
        return self.db.query(models.AgentReputation).options(
            joinedload(models.AgentReputation.agent)
        ).join(
            models.Agent
        ).filter(
            models.Agent.is_active == True
        ).order_by(
            desc(models.AgentReputation.trust_score)
        ).limit(limit).all()
