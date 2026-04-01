"""
ChannelRepository — data-access layer for PaymentChannel records.
"""

import hashlib
from datetime import datetime, timezone, timedelta
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

    # ------------------------------------------------------------------
    # Phase 3b — off-chain payment channel lifecycle
    # ------------------------------------------------------------------

    def open_with_deposit(
        self,
        channel_hash: str,
        agent_a_id: UUID,
        agent_b_id: UUID,
        deposit_a: Decimal,
        deposit_b: Decimal,
        settlement_period: int = 3600,
    ) -> models.PaymentChannel:
        """Create and open a channel with on-hub deposits.

        Sets balance_a=deposit_a, balance_b=deposit_b,
        capacity=deposit_a+deposit_b, status=OPEN, nonce=0.
        """
        capacity = deposit_a + deposit_b
        channel = models.PaymentChannel(
            channel_hash=channel_hash,
            agent_a_id=agent_a_id,
            agent_b_id=agent_b_id,
            capacity=capacity,
            status=models.ChannelStatus.OPEN,
            current_state={},
            deposit_a=deposit_a,
            deposit_b=deposit_b,
            balance_a=deposit_a,
            balance_b=deposit_b,
            nonce=0,
            settlement_period=settlement_period,
        )
        self.db.add(channel)
        self.db.flush()
        return channel

    def submit_update(
        self,
        channel_id: UUID,
        nonce: int,
        balance_a: Decimal,
        balance_b: Decimal,
        signature_a: Optional[str],
        signature_b: Optional[str],
    ) -> models.ChannelUpdate:
        """Store an off-chain state update record."""
        update = models.ChannelUpdate(
            channel_id=channel_id,
            nonce=nonce,
            balance_a=balance_a,
            balance_b=balance_b,
            signature_a=signature_a,
            signature_b=signature_b,
        )
        self.db.add(update)
        self.db.flush()
        return update

    def get_latest_update(self, channel_id: UUID) -> Optional[models.ChannelUpdate]:
        """Return the ChannelUpdate with the highest nonce, or None."""
        return (
            self.db.query(models.ChannelUpdate)
            .filter(models.ChannelUpdate.channel_id == channel_id)
            .order_by(desc(models.ChannelUpdate.nonce))
            .first()
        )

    def initiate_settlement(
        self,
        channel_id: UUID,
        nonce: int,
        balance_a: Decimal,
        balance_b: Decimal,
        sig_a: str,
        sig_b: str,
    ) -> int:
        """Transition OPEN -> CLOSING and record final balances.

        Sets closes_at = now + settlement_period.
        Returns number of rows updated.
        """
        channel = self.get_by_id(channel_id)
        closes_at = datetime.now(timezone.utc) + timedelta(
            seconds=channel.settlement_period if channel else 3600
        )
        rows = self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.id == channel_id,
            models.PaymentChannel.status == models.ChannelStatus.OPEN,
        ).update({
            "status": models.ChannelStatus.CLOSING,
            "nonce": nonce,
            "balance_a": balance_a,
            "balance_b": balance_b,
            "last_update_sig_a": sig_a,
            "last_update_sig_b": sig_b,
            "closes_at": closes_at,
        })
        return rows

    def settle(self, channel_id: UUID) -> int:
        """Transition CLOSING -> SETTLED. Returns rows updated."""
        rows = self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.id == channel_id,
            models.PaymentChannel.status == models.ChannelStatus.CLOSING,
        ).update({
            "status": models.ChannelStatus.SETTLED,
            "settled_at": datetime.now(timezone.utc),
        })
        return rows

    def finalize_close(self, channel_id: UUID) -> int:
        """Transition SETTLED -> CLOSED, set closed_at. Returns rows updated."""
        rows = self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.id == channel_id,
            models.PaymentChannel.status == models.ChannelStatus.SETTLED,
        ).update({
            "status": models.ChannelStatus.CLOSED,
            "closed_at": datetime.now(timezone.utc),
        })
        return rows

    def dispute(
        self,
        channel_id: UUID,
        nonce: int,
        balance_a: Decimal,
        balance_b: Decimal,
        sig_a: str,
        sig_b: str,
    ) -> int:
        """During CLOSING, submit a higher-nonce state to replace the current one.

        Returns rows updated.
        """
        rows = self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.id == channel_id,
            models.PaymentChannel.status == models.ChannelStatus.CLOSING,
        ).update({
            "nonce": nonce,
            "balance_a": balance_a,
            "balance_b": balance_b,
            "last_update_sig_a": sig_a,
            "last_update_sig_b": sig_b,
        })
        return rows

    def get_channels_ready_to_settle(self) -> List[models.PaymentChannel]:
        """Return all CLOSING channels whose closes_at is in the past."""
        now = datetime.now(timezone.utc)
        return (
            self.db.query(models.PaymentChannel)
            .filter(
                models.PaymentChannel.status == models.ChannelStatus.CLOSING,
                models.PaymentChannel.closes_at <= now,
            )
            .all()
        )
