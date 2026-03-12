"""
EscrowRepository — data-access layer for EscrowDeal records.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, List
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import desc

from . import models
from ._repo_base import _MAX_QUERY_LIMIT


class EscrowRepository:
    """Escrow deal data access"""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        deal_hash: str,
        buyer_id: UUID,
        seller_id: UUID,
        amount: Decimal,
        description: str,
        arbiter_id: Optional[UUID] = None,
        timeout_hours: int = 48,
        platform_fee_percent: Decimal = Decimal('0.01')
    ) -> models.EscrowDeal:
        """Create new escrow deal"""
        platform_fee_amount = amount * platform_fee_percent

        deal = models.EscrowDeal(
            deal_hash=deal_hash,
            buyer_id=buyer_id,
            seller_id=seller_id,
            arbiter_id=arbiter_id,
            amount=amount,
            description=description,
            timeout_hours=timeout_hours,
            platform_fee_percent=platform_fee_percent,
            platform_fee_amount=platform_fee_amount,
            status=models.EscrowStatus.PENDING,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=timeout_hours)
        )

        self.db.add(deal)
        return deal

    def get_by_id(self, deal_id: UUID) -> Optional[models.EscrowDeal]:
        """Get deal by ID"""
        return self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).first()

    def get_by_hash(self, deal_hash: str) -> Optional[models.EscrowDeal]:
        """Get deal by hash"""
        return self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.deal_hash == deal_hash
        ).first()

    def list_by_agent(
        self,
        agent_id: UUID,
        role: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[models.EscrowDeal]:
        """List deals where agent participates"""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(models.EscrowDeal)

        if role == 'buyer':
            query = query.filter(models.EscrowDeal.buyer_id == agent_id)
        elif role == 'seller':
            query = query.filter(models.EscrowDeal.seller_id == agent_id)
        elif role == 'arbiter':
            query = query.filter(models.EscrowDeal.arbiter_id == agent_id)
        else:
            query = query.filter(
                (models.EscrowDeal.buyer_id == agent_id) |
                (models.EscrowDeal.seller_id == agent_id) |
                (models.EscrowDeal.arbiter_id == agent_id)
            )

        if status:
            query = query.filter(models.EscrowDeal.status == status)

        return query.order_by(desc(models.EscrowDeal.created_at)).limit(limit).all()

    def fund_deal(self, deal_id: UUID, deposit_tx_hash: str, multisig_address: str):
        """Mark deal as funded"""
        self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).update({
            "status": models.EscrowStatus.FUNDED,
            "deposit_tx_hash": deposit_tx_hash,
            "multisig_address": multisig_address,
            "funded_at": datetime.now(timezone.utc)
        })

    def mark_delivered(self, deal_id: UUID):
        """Mark deal as delivered"""
        self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).update({
            "status": models.EscrowStatus.DELIVERED
        })

    def release(self, deal_id: UUID, release_tx_hash: str):
        """Release funds to seller"""
        self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).update({
            "status": models.EscrowStatus.COMPLETED,
            "release_tx_hash": release_tx_hash,
            "completed_at": datetime.now(timezone.utc)
        })

    def open_dispute(self, deal_id: UUID, reason: str, opened_by: UUID):
        """Open dispute on deal"""
        self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).update({
            "status": models.EscrowStatus.DISPUTED,
            "disputed_at": datetime.now(timezone.utc),
            "disputed_by": opened_by,
            "dispute_reason": reason
        })

    def arbitrate(self, deal_id: UUID, decision: str, arbiter_signature: str):
        """Arbiter makes decision"""
        updates = {
            "arbiter_decision": decision,
            "arbiter_signature": arbiter_signature
        }

        if decision == 'release':
            updates["status"] = models.EscrowStatus.COMPLETED
            updates["completed_at"] = datetime.now(timezone.utc)
        elif decision == 'refund':
            updates["status"] = models.EscrowStatus.REFUNDED
            updates["completed_at"] = datetime.now(timezone.utc)

        self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).update(updates)
