"""
Swap endpoints: rates, quote, create, status, claim.

All write endpoints require agent authentication via Bearer token.
GET /rates is public.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.swap_service import SwapService
from api.deps import get_current_agent
from api.schemas_swap import (
    SwapClaimRequest,
    SwapCreateRequest,
    SwapQuoteRequest,
)

logger = logging.getLogger("sthrip")
router = APIRouter(prefix="/v2/swap", tags=["swap"])

_svc = SwapService()


def _handle_service_error(exc: Exception) -> None:
    """Map SwapService exceptions to HTTP responses."""
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/rates")
def get_rates():
    """Return current exchange rates for all supported pairs (public)."""
    rates = _svc.get_rates()
    # Convert Decimal to str for JSON serialisation
    return {pair: str(rate) for pair, rate in rates.items()}


@router.post("/quote")
def get_quote(
    req: SwapQuoteRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Return a swap quote for the requested pair and amount."""
    try:
        return _svc.get_quote(req.from_currency, req.from_amount, req.to_currency)
    except (LookupError, PermissionError, ValueError) as exc:
        _handle_service_error(exc)


@router.post("/create", status_code=201)
def create_swap(
    req: SwapCreateRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Create a new cross-chain swap order."""
    with get_db() as db:
        try:
            return _svc.create_swap(
                db,
                from_agent_id=agent.id,
                from_currency=req.from_currency,
                from_amount=req.from_amount,
                to_currency=req.to_currency,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


@router.get("/{swap_id}")
def get_swap(
    swap_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Return the status of a swap order (only visible to the owning agent)."""
    with get_db() as db:
        try:
            return _svc.get_swap(db, order_id=swap_id, agent_id=agent.id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


@router.post("/{swap_id}/claim")
def claim_swap(
    swap_id: UUID,
    req: SwapClaimRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Claim a locked swap by revealing the HTLC pre-image secret."""
    with get_db() as db:
        try:
            return _svc.claim_swap(
                db,
                order_id=swap_id,
                agent_id=agent.id,
                htlc_secret=req.htlc_secret,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
