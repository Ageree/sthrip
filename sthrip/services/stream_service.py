"""
StreamService — real-time payment stream business logic.

Flow: start -> (pause <-> resume)* -> stop

Rules:
- Only agent_a of the channel can initiate a stream.
- Channel must be OPEN.
- rate * MIN_STREAM_DURATION must be <= channel.balance_a.
- Either participant (from_agent or to_agent) may pause/resume/stop.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.models import PaymentChannel, PaymentStream, ChannelStatus, StreamStatus
from sthrip.db.channel_repo import ChannelRepository
from sthrip.db.stream_repo import PaymentStreamRepository

logger = logging.getLogger("sthrip.stream")

# Minimum seconds a stream must be able to sustain given channel balance.
MIN_STREAM_DURATION: int = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _stream_to_dict(stream: PaymentStream) -> dict:
    """Convert a PaymentStream ORM object to an immutable dict."""
    state_val = stream.state.value if hasattr(stream.state, "value") else stream.state
    return {
        "stream_id": str(stream.id),
        "channel_id": str(stream.channel_id),
        "from_agent_id": str(stream.from_agent_id),
        "to_agent_id": str(stream.to_agent_id),
        "rate_per_second": str(stream.rate_per_second),
        "state": state_val,
        "started_at": _iso(stream.started_at),
        "paused_at": _iso(stream.paused_at),
        "stopped_at": _iso(stream.stopped_at),
        "total_streamed": str(stream.total_streamed),
    }


def _calculate_accrued(stream: PaymentStream) -> Decimal:
    """Calculate the accrued amount for a stream based on its current state."""
    rate = Decimal(str(stream.rate_per_second))

    if stream.state == StreamStatus.STOPPED:
        return Decimal(str(stream.total_streamed))

    if stream.state == StreamStatus.PAUSED:
        started = stream.started_at
        paused = stream.paused_at
        if started is None or paused is None:
            return Decimal("0")
        # Ensure both are timezone-aware for comparison
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if paused.tzinfo is None:
            paused = paused.replace(tzinfo=timezone.utc)
        elapsed = (paused - started).total_seconds()
        return rate * Decimal(str(max(0.0, elapsed)))

    # ACTIVE state
    started = stream.started_at
    if started is None:
        return Decimal("0")
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = (_now() - started).total_seconds()
    return rate * Decimal(str(max(0.0, elapsed)))


class StreamService:
    """Business logic for payment streams."""

    def start_stream(
        self,
        db: Session,
        channel_id: str,
        from_agent_id: str,
        rate_per_second: Decimal,
    ) -> dict:
        """Create and start a new payment stream.

        Raises:
            LookupError: Channel not found.
            ValueError: Channel not OPEN, or rate exceeds sustainable threshold.
            PermissionError: from_agent is not agent_a of the channel.
        """
        channel_repo = ChannelRepository(db)
        channel = channel_repo.get_by_id(UUID(str(channel_id)))
        if channel is None:
            raise LookupError(f"Channel {channel_id} not found")

        if channel.status != ChannelStatus.OPEN:
            raise ValueError(
                f"Channel must be OPEN to start a stream (current: {channel.status})"
            )

        if str(channel.agent_a_id) != str(from_agent_id):
            raise PermissionError(
                "Only agent_a of the channel can initiate a stream"
            )

        balance_a = Decimal(str(channel.balance_a))
        min_required = rate_per_second * MIN_STREAM_DURATION
        if min_required > balance_a:
            raise ValueError(
                f"Rate {rate_per_second}/s requires {min_required} to sustain for "
                f"{MIN_STREAM_DURATION}s, but channel.balance_a={balance_a}"
            )

        stream_repo = PaymentStreamRepository(db)
        to_agent_id = channel.agent_b_id
        stream = stream_repo.create(
            channel_id=channel.id,
            from_agent_id=UUID(str(from_agent_id)),
            to_agent_id=to_agent_id,
            rate_per_second=rate_per_second,
        )
        db.flush()

        logger.info(
            "Stream started: id=%s channel=%s rate=%s",
            stream.id,
            channel_id,
            rate_per_second,
        )
        return _stream_to_dict(stream)

    def get_accrued(self, db: Session, stream_id: str) -> dict:
        """Return stream info plus the currently accrued amount.

        Raises:
            LookupError: Stream not found.
        """
        repo = PaymentStreamRepository(db)
        stream = repo.get_by_id(UUID(str(stream_id)))
        if stream is None:
            raise LookupError(f"Stream {stream_id} not found")

        accrued = _calculate_accrued(stream)
        result = _stream_to_dict(stream)
        result["accrued"] = str(accrued)
        return result

    def pause_stream(self, db: Session, stream_id: str, agent_id: str) -> dict:
        """Pause an ACTIVE stream.

        Raises:
            LookupError: Stream not found.
            PermissionError: agent_id is not a participant.
            ValueError: Stream is not in ACTIVE state.
        """
        repo = PaymentStreamRepository(db)
        stream = repo.get_by_id(UUID(str(stream_id)))
        if stream is None:
            raise LookupError(f"Stream {stream_id} not found")

        _verify_participant(stream, agent_id)

        rows = repo.pause(stream.id)
        if rows == 0:
            raise ValueError(f"Stream {stream_id} is not ACTIVE (state: {stream.state})")

        db.flush()
        refreshed = repo.get_by_id(stream.id)
        return _stream_to_dict(refreshed)

    def resume_stream(self, db: Session, stream_id: str, agent_id: str) -> dict:
        """Resume a PAUSED stream.

        Raises:
            LookupError: Stream not found.
            PermissionError: agent_id is not a participant.
            ValueError: Stream is not in PAUSED state.
        """
        repo = PaymentStreamRepository(db)
        stream = repo.get_by_id(UUID(str(stream_id)))
        if stream is None:
            raise LookupError(f"Stream {stream_id} not found")

        _verify_participant(stream, agent_id)

        rows = repo.resume(stream.id)
        if rows == 0:
            raise ValueError(f"Stream {stream_id} is not PAUSED (state: {stream.state})")

        db.flush()
        refreshed = repo.get_by_id(stream.id)
        return _stream_to_dict(refreshed)

    def stop_stream(self, db: Session, stream_id: str, agent_id: str) -> dict:
        """Stop a stream, calculating and recording the final accrued total.

        Raises:
            LookupError: Stream not found.
            PermissionError: agent_id is not a participant.
            ValueError: Stream is already STOPPED.
        """
        repo = PaymentStreamRepository(db)
        stream = repo.get_by_id(UUID(str(stream_id)))
        if stream is None:
            raise LookupError(f"Stream {stream_id} not found")

        _verify_participant(stream, agent_id)

        if stream.state == StreamStatus.STOPPED:
            raise ValueError(f"Stream {stream_id} is already STOPPED")

        total = _calculate_accrued(stream)
        rows = repo.stop(stream.id, total_streamed=total)
        if rows == 0:
            raise ValueError(f"Stream {stream_id} could not be stopped")

        db.flush()
        refreshed = repo.get_by_id(stream.id)
        result = _stream_to_dict(refreshed)
        result["total_streamed"] = str(total)
        return result


def _verify_participant(stream: PaymentStream, agent_id: str) -> None:
    """Raise PermissionError if agent_id is not from_agent or to_agent."""
    if str(stream.from_agent_id) != str(agent_id) and str(stream.to_agent_id) != str(agent_id):
        raise PermissionError(
            f"Agent {agent_id} is not a participant of stream {stream.id}"
        )
