"""
PaymentStreamRepository — data-access layer for PaymentStream records.

State machine:
    ACTIVE -> PAUSED  (pause)
    PAUSED -> ACTIVE  (resume)
    ACTIVE | PAUSED -> STOPPED  (stop)
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from . import models
from .models import StreamStatus
from ._repo_base import _MAX_QUERY_LIMIT


class PaymentStreamRepository:
    """Data access for payment streams."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        channel_id: UUID,
        from_agent_id: UUID,
        to_agent_id: UUID,
        rate_per_second: Decimal,
    ) -> models.PaymentStream:
        """Create a new ACTIVE stream."""
        stream = models.PaymentStream(
            channel_id=channel_id,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            rate_per_second=rate_per_second,
            started_at=datetime.now(timezone.utc),
            state=StreamStatus.ACTIVE,
        )
        self.db.add(stream)
        self.db.flush()
        return stream

    def get_by_id(self, stream_id: UUID) -> Optional[models.PaymentStream]:
        """Get a stream by primary key."""
        return (
            self.db.query(models.PaymentStream)
            .filter(models.PaymentStream.id == stream_id)
            .first()
        )

    def get_by_channel(self, channel_id: UUID) -> List[models.PaymentStream]:
        """Return all ACTIVE streams on a channel."""
        return (
            self.db.query(models.PaymentStream)
            .filter(
                models.PaymentStream.channel_id == channel_id,
                models.PaymentStream.state == StreamStatus.ACTIVE,
            )
            .all()
        )

    def pause(self, stream_id: UUID) -> int:
        """Transition ACTIVE -> PAUSED, set paused_at=now.

        Returns number of rows updated (0 if stream was not ACTIVE).
        """
        now = datetime.now(timezone.utc)
        return (
            self.db.query(models.PaymentStream)
            .filter(
                models.PaymentStream.id == stream_id,
                models.PaymentStream.state == StreamStatus.ACTIVE,
            )
            .update({"state": StreamStatus.PAUSED, "paused_at": now})
        )

    def resume(self, stream_id: UUID) -> int:
        """Transition PAUSED -> ACTIVE, clear paused_at.

        Returns number of rows updated (0 if stream was not PAUSED).
        """
        return (
            self.db.query(models.PaymentStream)
            .filter(
                models.PaymentStream.id == stream_id,
                models.PaymentStream.state == StreamStatus.PAUSED,
            )
            .update({"state": StreamStatus.ACTIVE, "paused_at": None})
        )

    def stop(self, stream_id: UUID, total_streamed: Decimal) -> int:
        """Transition any non-STOPPED state -> STOPPED.

        Sets stopped_at=now and records total_streamed.
        Returns number of rows updated.
        """
        now = datetime.now(timezone.utc)
        return (
            self.db.query(models.PaymentStream)
            .filter(
                models.PaymentStream.id == stream_id,
                models.PaymentStream.state != StreamStatus.STOPPED,
            )
            .update(
                {
                    "state": StreamStatus.STOPPED,
                    "stopped_at": now,
                    "total_streamed": total_streamed,
                }
            )
        )

    def list_by_agent(
        self,
        agent_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[models.PaymentStream], int]:
        """List streams where agent is sender or recipient.

        Returns (items, total_count).
        """
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(models.PaymentStream).filter(
            (models.PaymentStream.from_agent_id == agent_id)
            | (models.PaymentStream.to_agent_id == agent_id)
        )
        total = query.count()
        items = (
            query.order_by(models.PaymentStream.started_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return items, total
