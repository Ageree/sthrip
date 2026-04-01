"""
Payment Streams endpoints.

All routes require an authenticated agent (Depends(get_current_agent)).
"""

import logging

from fastapi import APIRouter, HTTPException, Depends

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.stream_service import StreamService
from api.deps import get_current_agent
from api.schemas_streams import StreamStartRequest, StreamResponse

logger = logging.getLogger("sthrip.streams")
router = APIRouter(prefix="/v2/streams", tags=["streams"])

_svc = StreamService()


def _handle_error(exc: Exception) -> None:
    """Map StreamService exceptions to appropriate HTTP status codes."""
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.post("", status_code=201, response_model=StreamResponse)
def start_stream(
    req: StreamStartRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Start a new payment stream on an OPEN channel.

    The authenticated agent must be agent_a of the channel.
    """
    with get_db() as db:
        try:
            result = _svc.start_stream(
                db=db,
                channel_id=req.channel_id,
                from_agent_id=str(agent.id),
                rate_per_second=req.rate_per_second,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_error(exc)

    return result


@router.get("/{stream_id}", response_model=StreamResponse)
def get_stream(
    stream_id: str,
    agent: Agent = Depends(get_current_agent),
):
    """Get stream details including the currently accrued amount."""
    with get_db() as db:
        try:
            result = _svc.get_accrued(db=db, stream_id=stream_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    return result


@router.post("/{stream_id}/pause", response_model=StreamResponse)
def pause_stream(
    stream_id: str,
    agent: Agent = Depends(get_current_agent),
):
    """Pause an ACTIVE stream."""
    with get_db() as db:
        try:
            result = _svc.pause_stream(db=db, stream_id=stream_id, agent_id=str(agent.id))
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_error(exc)

    return result


@router.post("/{stream_id}/resume", response_model=StreamResponse)
def resume_stream(
    stream_id: str,
    agent: Agent = Depends(get_current_agent),
):
    """Resume a PAUSED stream."""
    with get_db() as db:
        try:
            result = _svc.resume_stream(db=db, stream_id=stream_id, agent_id=str(agent.id))
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_error(exc)

    return result


@router.post("/{stream_id}/stop", response_model=StreamResponse)
def stop_stream(
    stream_id: str,
    agent: Agent = Depends(get_current_agent),
):
    """Stop a stream and record the final accrued total."""
    with get_db() as db:
        try:
            result = _svc.stop_stream(db=db, stream_id=stream_id, agent_id=str(agent.id))
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_error(exc)

    return result
