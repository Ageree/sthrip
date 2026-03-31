"""
SpendingPolicyRepository — data-access layer for per-agent spending policies.
"""

from decimal import Decimal
from typing import Optional, List
from uuid import UUID

from sqlalchemy.orm import Session

from .models import SpendingPolicy


class SpendingPolicyRepository:
    """CRUD operations for SpendingPolicy records."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_agent_id(self, agent_id: UUID) -> Optional[SpendingPolicy]:
        """Return the active spending policy for an agent, or None."""
        return (
            self.db.query(SpendingPolicy)
            .filter(SpendingPolicy.agent_id == agent_id)
            .first()
        )

    def upsert(
        self,
        agent_id: UUID,
        *,
        max_per_tx: Optional[Decimal] = None,
        max_per_session: Optional[Decimal] = None,
        daily_limit: Optional[Decimal] = None,
        allowed_agents: Optional[List[str]] = None,
        blocked_agents: Optional[List[str]] = None,
        require_escrow_above: Optional[Decimal] = None,
    ) -> SpendingPolicy:
        """Create or update the spending policy for *agent_id*.

        Returns a new SpendingPolicy instance (immutable semantics at the
        caller level — the ORM object is mutated internally by SQLAlchemy's
        unit-of-work pattern, which is the accepted exception to project
        immutability guidelines).
        """
        existing = self.get_by_agent_id(agent_id)

        if existing is not None:
            existing.max_per_tx = max_per_tx
            existing.max_per_session = max_per_session
            existing.daily_limit = daily_limit
            existing.allowed_agents = allowed_agents
            existing.blocked_agents = blocked_agents
            existing.require_escrow_above = require_escrow_above
            self.db.flush()
            return existing

        policy = SpendingPolicy(
            agent_id=agent_id,
            max_per_tx=max_per_tx,
            max_per_session=max_per_session,
            daily_limit=daily_limit,
            allowed_agents=allowed_agents,
            blocked_agents=blocked_agents,
            require_escrow_above=require_escrow_above,
        )
        self.db.add(policy)
        self.db.flush()
        return policy
