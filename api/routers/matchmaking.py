"""Matchmaking endpoints: create request, get result, accept match."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.matchmaking_service import MatchmakingService
from api.deps import get_current_agent
from api.schemas_matchmaking import MatchRequestCreate, MatchRequestResponse

logger = logging.getLogger("sthrip")
router = APIRouter(prefix="/v2/matchmaking", tags=["matchmaking"])

_svc = MatchmakingService()


def _handle_service_error(exc: Exception) -> None:
    """Map MatchmakingService exceptions to HTTP responses."""
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.post("/request", status_code=201, response_model=MatchRequestResponse)
async def create_match_request(
    req: MatchRequestCreate,
    agent: Agent = Depends(get_current_agent),
):
    """Submit a new matchmaking request.

    The hub immediately searches for the best qualifying agent. The response
    state is ``searching`` if no agent qualifies immediately, ``matched`` when
    a suitable agent is found (but not yet accepted), or ``assigned`` when
    ``auto_assign=True`` and an SLA contract was automatically created.
    """
    with get_db() as db:
        try:
            result = _svc.create_request(
                db=db,
                requester_id=agent.id,
                task_description=req.task_description,
                required_capabilities=req.required_capabilities,
                budget=req.budget,
                currency=req.currency,
                deadline_secs=req.deadline_secs,
                min_rating=req.min_rating,
                auto_assign=req.auto_assign,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return result


@router.get("/{request_id}", response_model=MatchRequestResponse)
async def get_match_result(
    request_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Retrieve the current state of a matchmaking request.

    Only the original requester may access the request.
    """
    with get_db() as db:
        from sthrip.db.matchmaking_repo import MatchmakingRepository
        repo = MatchmakingRepository(db)
        req = repo.get_by_id(request_id)
        if req is None:
            raise HTTPException(status_code=404, detail="Match request not found")
        if req.requester_id != agent.id:
            raise HTTPException(status_code=403, detail="Access denied: not the requester")

        from sthrip.services.matchmaking_service import _request_to_dict
        return _request_to_dict(req)


@router.post("/{request_id}/accept", response_model=MatchRequestResponse)
async def accept_match(
    request_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Accept the matched agent and auto-create an SLA contract.

    Only the original requester may accept. The request must be in
    ``matched`` state (i.e. a candidate was found but not yet accepted).
    """
    with get_db() as db:
        try:
            result = _svc.accept_match(
                db=db,
                request_id=request_id,
                requester_id=agent.id,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return result
