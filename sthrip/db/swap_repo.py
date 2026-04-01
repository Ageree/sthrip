"""
SwapRepository — data-access layer for cross-chain SwapOrder records.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from . import models
from .models import SwapStatus
from ._repo_base import _MAX_QUERY_LIMIT


class SwapRepository:
    """Data access for SwapOrder records."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        from_agent_id: UUID,
        from_currency: str,
        from_amount: Decimal,
        to_currency: str,
        to_amount: Decimal,
        exchange_rate: Decimal,
        fee_amount: Decimal,
        htlc_hash: str,
        lock_expiry: datetime,
    ) -> models.SwapOrder:
        """Persist a new SwapOrder in CREATED state."""
        order = models.SwapOrder(
            from_agent_id=from_agent_id,
            from_currency=from_currency,
            from_amount=from_amount,
            to_currency=to_currency,
            to_amount=to_amount,
            exchange_rate=exchange_rate,
            fee_amount=fee_amount,
            state=SwapStatus.CREATED,
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )
        self.db.add(order)
        self.db.flush()
        return order

    def get_by_id(self, swap_id: UUID) -> Optional[models.SwapOrder]:
        """Return SwapOrder for the given id, or None."""
        return (
            self.db.query(models.SwapOrder)
            .filter(models.SwapOrder.id == swap_id)
            .first()
        )

    def lock(self, swap_id: UUID, btc_tx_hash: str) -> int:
        """Transition CREATED → LOCKED, storing btc_tx_hash.

        Returns rows updated (0 if state guard prevented the update).
        """
        return (
            self.db.query(models.SwapOrder)
            .filter(
                models.SwapOrder.id == swap_id,
                models.SwapOrder.state == SwapStatus.CREATED,
            )
            .update(
                {
                    "state": SwapStatus.LOCKED,
                    "btc_tx_hash": btc_tx_hash,
                }
            )
        )

    def complete(
        self,
        swap_id: UUID,
        htlc_secret: str,
        xmr_tx_hash: Optional[str] = None,
    ) -> int:
        """Transition LOCKED → COMPLETED, storing htlc_secret and optional xmr_tx_hash.

        Returns rows updated (0 if state guard prevented the update).
        """
        update_values = {
            "state": SwapStatus.COMPLETED,
            "htlc_secret": htlc_secret,
        }
        if xmr_tx_hash is not None:
            update_values["xmr_tx_hash"] = xmr_tx_hash

        return (
            self.db.query(models.SwapOrder)
            .filter(
                models.SwapOrder.id == swap_id,
                models.SwapOrder.state == SwapStatus.LOCKED,
            )
            .update(update_values)
        )

    def refund(self, swap_id: UUID) -> int:
        """Transition LOCKED → REFUNDED.

        Returns rows updated (0 if state guard prevented the update).
        """
        return (
            self.db.query(models.SwapOrder)
            .filter(
                models.SwapOrder.id == swap_id,
                models.SwapOrder.state == SwapStatus.LOCKED,
            )
            .update({"state": SwapStatus.REFUNDED})
        )

    def expire(self, swap_id: UUID) -> int:
        """Transition CREATED or LOCKED → EXPIRED.

        Returns rows updated (0 if state guard prevented the update).
        """
        return (
            self.db.query(models.SwapOrder)
            .filter(
                models.SwapOrder.id == swap_id,
                models.SwapOrder.state.in_(
                    [SwapStatus.CREATED, SwapStatus.LOCKED]
                ),
            )
            .update({"state": SwapStatus.EXPIRED})
        )

    def get_expired(self) -> List[models.SwapOrder]:
        """Return orders that have passed lock_expiry and are still CREATED or LOCKED."""
        now = datetime.now(timezone.utc)
        return (
            self.db.query(models.SwapOrder)
            .filter(
                models.SwapOrder.state.in_(
                    [SwapStatus.CREATED, SwapStatus.LOCKED]
                ),
                models.SwapOrder.lock_expiry <= now,
            )
            .all()
        )

    def list_by_agent(
        self,
        agent_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[models.SwapOrder], int]:
        """List swap orders for an agent.  Returns (items, total_count)."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(models.SwapOrder).filter(
            models.SwapOrder.from_agent_id == agent_id
        )
        total = query.count()
        items = (
            query.order_by(models.SwapOrder.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return items, total
