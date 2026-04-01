"""Reviews endpoints: create, list, rating summary, ZK proof."""

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.review_service import ReviewService
from api.deps import get_current_agent
from api.schemas_reviews import (
    ReviewCreateRequest,
    ReviewProofRequest,
    ReviewProofResponse,
    ReviewProofVerifyRequest,
    ReviewProofVerifyResponse,
)

logger = logging.getLogger("sthrip")
router = APIRouter(prefix="/v2", tags=["reviews"])

_svc = ReviewService()


def _handle_service_error(exc: Exception) -> None:
    """Map ReviewService exceptions to HTTP responses."""
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


def _resolve_agent_by_name(db, agent_name: str) -> Agent:
    """Return agent by name or raise 404."""
    agent = db.query(Agent).filter(Agent.agent_name == agent_name).first()
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_name!r} not found")
    return agent


@router.post("/agents/{agent_name}/reviews", status_code=201)
async def create_review(
    agent_name: str,
    req: ReviewCreateRequest,
    current_agent: Agent = Depends(get_current_agent),
):
    """Leave a review for an agent. Authentication required (reviewer = current agent).

    The transaction_id must refer to an existing transaction of the given type.
    """
    with get_db() as db:
        reviewed_agent = _resolve_agent_by_name(db, agent_name)

        try:
            import uuid
            result = _svc.create_review(
                db=db,
                reviewer_id=current_agent.id,
                reviewed_id=reviewed_agent.id,
                transaction_id=uuid.UUID(req.transaction_id),
                transaction_type=req.transaction_type,
                overall_rating=req.overall_rating,
                speed_rating=req.speed_rating,
                quality_rating=req.quality_rating,
                reliability_rating=req.reliability_rating,
                comment=req.comment,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    return result


@router.get("/agents/{agent_name}/reviews")
async def get_reviews(
    agent_name: str,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Get reviews for an agent (public endpoint, no authentication required)."""
    with get_db() as db:
        reviewed_agent = _resolve_agent_by_name(db, agent_name)

        try:
            result = _svc.get_reviews(
                db=db,
                agent_id=reviewed_agent.id,
                limit=limit,
                offset=offset,
            )
        except (LookupError, ValueError) as exc:
            _handle_service_error(exc)

    return result


@router.get("/agents/{agent_name}/ratings")
async def get_ratings(agent_name: str):
    """Get rating summary for an agent (public endpoint, no authentication required)."""
    with get_db() as db:
        reviewed_agent = _resolve_agent_by_name(db, agent_name)

        try:
            result = _svc.get_rating_summary(db=db, agent_id=reviewed_agent.id)
        except (LookupError, ValueError) as exc:
            _handle_service_error(exc)

    return result


@router.post("/me/review-proof", response_model=ReviewProofResponse)
async def generate_review_proof(
    req: ReviewProofRequest,
    current_agent: Agent = Depends(get_current_agent),
):
    """Generate a ZK proof that the authenticated agent meets review thresholds.

    The proof attests to both a minimum number of reviews and a minimum
    average overall rating without revealing the actual values.
    """
    with get_db() as db:
        try:
            result = _svc.generate_review_proof(
                db=db,
                agent_id=current_agent.id,
                min_reviews=req.min_reviews,
                min_avg=req.min_avg,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    return result


@router.post("/review-proof/verify", response_model=ReviewProofVerifyResponse)
async def verify_review_proof(req: ReviewProofVerifyRequest):
    """Verify a ZK review proof (public endpoint, no authentication required).

    Returns ``{"valid": true}`` when the proof is cryptographically valid
    for the given thresholds, ``{"valid": false}`` otherwise.
    """
    valid = _svc.verify_review_proof(
        commitment=req.commitment,
        proof=req.proof,
        min_reviews=req.min_reviews,
        min_avg=req.min_avg,
    )
    return {"valid": valid}
