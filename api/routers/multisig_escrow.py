"""Multisig escrow endpoints: round submission, state query, cosign, dispute."""

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.multisig_coordinator import MultisigCoordinator
from api.deps import get_current_agent
from api.schemas import (
    MultisigRoundRequest,
    MultisigStateResponse,
    CosignRequest,
    DisputeRequest,
)

logger = logging.getLogger("sthrip.multisig")
router = APIRouter(prefix="/v2/escrow", tags=["multisig-escrow"])

_coordinator = MultisigCoordinator()


def _handle_service_error(exc: Exception) -> None:
    """Map service exceptions to HTTP responses."""
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.post("/{escrow_id}/round", status_code=200)
async def submit_round(
    escrow_id: UUID,
    req: MultisigRoundRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Submit multisig key exchange data for a setup round."""
    with get_db() as db:
        try:
            result = _coordinator.submit_round(
                db=db,
                escrow_id=escrow_id,
                participant=req.participant,
                round_number=req.round_number,
                multisig_info=req.multisig_info,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return result


@router.get("/{escrow_id}/multisig-state")
async def get_multisig_state(
    escrow_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Get current multisig escrow state."""
    with get_db() as db:
        try:
            result = _coordinator.get_state(db=db, escrow_id=escrow_id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return result


@router.post("/{escrow_id}/cosign")
async def cosign_release(
    escrow_id: UUID,
    req: CosignRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Cosign a partially-signed release transaction (2nd of 2 required)."""
    with get_db() as db:
        try:
            result = _coordinator.cosign_release(
                db=db,
                escrow_id=escrow_id,
                signer=req.signer,
                signed_tx=req.signed_tx,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return result


@router.post("/{escrow_id}/dispute")
async def dispute_escrow(
    escrow_id: UUID,
    req: DisputeRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Raise a dispute on a multisig escrow. Hub mediates resolution."""
    with get_db() as db:
        try:
            result = _coordinator.dispute(
                db=db,
                escrow_id=escrow_id,
                disputer=req.disputer,
                reason=req.reason,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return result
