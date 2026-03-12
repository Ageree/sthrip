"""
TransactionRepository — data-access layer for Transaction records.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import desc, func, and_

from . import models
from ._repo_base import _MAX_QUERY_LIMIT


class TransactionRepository:
    """Transaction data access"""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        tx_hash: str,
        network: str,
        from_agent_id: Optional[UUID],
        to_agent_id: Optional[UUID],
        amount: Decimal,
        token: str = "XMR",
        payment_type: str = "p2p",
        status: str = "pending",
        fee: Decimal = Decimal('0'),
        fee_collected: Decimal = Decimal('0'),
        memo: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> models.Transaction:
        """Record new transaction"""
        tx = models.Transaction(
            tx_hash=tx_hash,
            network=network,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            amount=amount,
            token=token,
            payment_type=payment_type,
            status=status,
            fee=fee,
            fee_collected=fee_collected,
            memo=memo,
            metadata=metadata or {}
        )
        self.db.add(tx)
        return tx

    def get_by_hash(self, tx_hash: str) -> Optional[models.Transaction]:
        """Get transaction by hash"""
        return self.db.query(models.Transaction).filter(
            models.Transaction.tx_hash == tx_hash
        ).first()

    def _agent_direction_filter(self, query, agent_id: UUID, direction: Optional[str]):
        """Apply direction filter to a transaction query."""
        if direction == 'in':
            return query.filter(models.Transaction.to_agent_id == agent_id)
        elif direction == 'out':
            return query.filter(models.Transaction.from_agent_id == agent_id)
        return query.filter(
            (models.Transaction.from_agent_id == agent_id) |
            (models.Transaction.to_agent_id == agent_id)
        )

    def count_by_agent(self, agent_id: UUID, direction: Optional[str] = None) -> int:
        """Count transactions for agent."""
        query = self.db.query(func.count(models.Transaction.id))
        query = self._agent_direction_filter(query, agent_id, direction)
        return query.scalar() or 0

    def list_by_agent(
        self,
        agent_id: UUID,
        direction: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[models.Transaction]:
        """List transactions for agent"""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(models.Transaction)

        if direction == 'in':
            query = query.filter(models.Transaction.to_agent_id == agent_id)
        elif direction == 'out':
            query = query.filter(models.Transaction.from_agent_id == agent_id)
        else:
            query = query.filter(
                (models.Transaction.from_agent_id == agent_id) |
                (models.Transaction.to_agent_id == agent_id)
            )

        return query.order_by(desc(models.Transaction.created_at)).offset(offset).limit(limit).all()

    def confirm_transaction(
        self,
        tx_hash: str,
        block_number: int,
        confirmations: int = 1
    ):
        """Mark transaction as confirmed"""
        self.db.query(models.Transaction).filter(
            models.Transaction.tx_hash == tx_hash
        ).update({
            "status": models.TransactionStatus.CONFIRMED,
            "block_number": block_number,
            "confirmations": confirmations,
            "confirmed_at": datetime.now(timezone.utc)
        })

    def get_volume_by_agent(self, agent_id: UUID, days: int = 30) -> Decimal:
        """Get total volume for agent in last N days"""
        since = datetime.now(timezone.utc) - timedelta(days=days)

        result = self.db.query(func.sum(models.Transaction.amount)).filter(
            and_(
                (models.Transaction.from_agent_id == agent_id) |
                (models.Transaction.to_agent_id == agent_id),
                models.Transaction.status == models.TransactionStatus.CONFIRMED,
                models.Transaction.created_at >= since
            )
        ).scalar()

        return result or Decimal('0')
