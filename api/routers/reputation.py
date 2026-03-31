"""Reputation proof endpoints: generate and verify ZK reputation proofs."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from sthrip.db.database import get_db
from sthrip.db.models import Agent, AgentReputation
from sthrip.services.zk_reputation_service import ZKReputationService
from api.deps import get_current_agent
from api.schemas import (
    ReputationProofRequest,
    ReputationProofResponse,
    ReputationVerifyRequest,
    ReputationVerifyResponse,
)

logger = logging.getLogger("sthrip")
router = APIRouter(prefix="/v2", tags=["reputation"])

_zk_service = ZKReputationService()


@router.post("/me/reputation-proof", response_model=ReputationProofResponse)
async def generate_reputation_proof(
    req: ReputationProofRequest,
    agent: Agent = Depends(get_current_agent),
) -> ReputationProofResponse:
    """Generate a ZK proof that the authenticated agent's trust score >= threshold."""
    with get_db() as db:
        reputation = (
            db.query(AgentReputation)
            .filter(AgentReputation.agent_id == agent.id)
            .first()
        )

        if reputation is None:
            raise HTTPException(
                status_code=404,
                detail="No reputation record found for this agent",
            )

        score: int = reputation.trust_score

        # Create or refresh the commitment if it does not exist yet
        if not reputation.reputation_commitment or not reputation.reputation_blinding:
            commitment, blinding = _zk_service.create_commitment(score)
            reputation.reputation_commitment = commitment
            reputation.reputation_blinding = blinding
            db.commit()
            db.refresh(reputation)
        else:
            # Re-derive commitment when the trust_score has changed since
            # the last commitment was stored
            stored_commitment = reputation.reputation_commitment
            blinding = reputation.reputation_blinding
            expected = _zk_service._compute_commitment(score, blinding)
            if stored_commitment != expected:
                commitment, blinding = _zk_service.create_commitment(score)
                reputation.reputation_commitment = commitment
                reputation.reputation_blinding = blinding
                db.commit()
                db.refresh(reputation)

        commitment = reputation.reputation_commitment
        blinding = reputation.reputation_blinding

        try:
            proof = _zk_service.generate_proof(score, blinding, req.threshold)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        return ReputationProofResponse(
            commitment=commitment,
            proof=proof,
            threshold=req.threshold,
        )


@router.post("/verify-reputation", response_model=ReputationVerifyResponse)
async def verify_reputation_proof(
    req: ReputationVerifyRequest,
) -> ReputationVerifyResponse:
    """Verify a ZK reputation proof. Public endpoint, no authentication required."""
    valid = _zk_service.verify_proof(req.commitment, req.proof, req.threshold)
    return ReputationVerifyResponse(valid=valid)
