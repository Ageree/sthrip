"""
ChannelRepository — data-access layer for PaymentChannel records.
"""

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import desc

from . import models
from ._repo_base import _MAX_QUERY_LIMIT


class ChannelRepository:
    """Payment channel data access"""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        channel_hash: str,
        agent_a_id: UUID,
        agent_b_id: UUID,
        capacity: Decimal,
        initial_state: Dict[str, Any]
    ) -> models.PaymentChannel:
        """Create new channel"""
        channel = models.PaymentChannel(
            channel_hash=channel_hash,
            agent_a_id=agent_a_id,
            agent_b_id=agent_b_id,
            capacity=capacity,
            status=models.ChannelStatus.PENDING,
            current_state=initial_state
        )

        self.db.add(channel)
        return channel

    def get_by_id(self, channel_id: UUID) -> Optional[models.PaymentChannel]:
        """Get channel by ID"""
        return self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.id == channel_id
        ).first()

    def get_by_hash(self, channel_hash: str) -> Optional[models.PaymentChannel]:
        """Get channel by hash"""
        return self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.channel_hash == channel_hash
        ).first()

    def list_by_agent(
        self,
        agent_id: UUID,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[models.PaymentChannel]:
        """List channels for agent"""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(models.PaymentChannel).filter(
            (models.PaymentChannel.agent_a_id == agent_id) |
            (models.PaymentChannel.agent_b_id == agent_id)
        )

        if status:
            query = query.filter(models.PaymentChannel.status == status)

        return query.order_by(desc(models.PaymentChannel.created_at)).limit(limit).all()

    def fund_channel(self, channel_id: UUID, funding_tx_hash: str, multisig_address: str):
        """Mark channel as funded"""
        self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.id == channel_id
        ).update({
            "status": models.ChannelStatus.OPEN,
            "funding_tx_hash": funding_tx_hash,
            "multisig_address": multisig_address,
            "funded_at": datetime.now(timezone.utc)
        })

    def update_state(
        self,
        channel_id: UUID,
        sequence_number: int,
        balance_a: Decimal,
        balance_b: Decimal,
        signature_a: Optional[str] = None,
        signature_b: Optional[str] = None
    ):
        """Update channel state"""
        new_state = {
            "sequence_number": sequence_number,
            "balance_a": str(balance_a),
            "balance_b": str(balance_b),
            "signature_a": signature_a,
            "signature_b": signature_b,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.id == channel_id
        ).update({
            "current_state": new_state
        })

        state_hash = hashlib.sha256(
            f"{sequence_number}:{balance_a}:{balance_b}".encode()
        ).hexdigest()

        state_record = models.ChannelState(
            channel_id=channel_id,
            sequence_number=sequence_number,
            balance_a=balance_a,
            balance_b=balance_b,
            signature_a=signature_a,
            signature_b=signature_b,
            state_hash=state_hash
        )
        self.db.add(state_record)

    def close_channel(self, channel_id: UUID, closing_tx_hash: str):
        """Mark channel as closed"""
        self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.id == channel_id
        ).update({
            "status": models.ChannelStatus.CLOSED,
            "closing_tx_hash": closing_tx_hash,
            "closed_at": datetime.now(timezone.utc)
        })
