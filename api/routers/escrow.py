"""Escrow endpoints: create, accept, deliver, release, cancel, detail, list."""

import logging
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.escrow_service import EscrowService
from sthrip.services.multisig_coordinator import MultisigCoordinator
from sthrip.services.webhook_service import queue_webhook
from api.deps import get_current_agent
from api.schemas import (
    EscrowCreateRequest,
    EscrowReleaseRequest,
    MilestoneReleaseRequest,
)

logger = logging.getLogger("sthrip")
router = APIRouter(prefix="/v2/escrow", tags=["escrow"])

_svc = EscrowService()
_multisig = MultisigCoordinator()


def _handle_service_error(exc: Exception):
    """Map EscrowService exceptions to HTTP responses."""
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


def _lookup_active_seller(db, name: str) -> Agent:
    """Resolve seller by name inside the given session."""
    seller = db.query(Agent).filter(Agent.agent_name == name).first()
    if not seller:
        raise HTTPException(status_code=404, detail="Seller agent not found")
    if not seller.is_active:
        raise HTTPException(status_code=400, detail="Seller agent is not active")
    return seller


@router.post("", status_code=201)
async def create_escrow(
    req: EscrowCreateRequest,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_current_agent),
):
    """Create a new escrow deal. The authenticated agent becomes the buyer.

    Supports two modes:
      - hub-held (default): funds locked in hub wallet, hub controls release.
      - multisig: 2-of-3 Monero multisig wallet (buyer, seller, hub).
    """
    with get_db() as db:
        seller = _lookup_active_seller(db, req.seller_agent_name)
        if seller.id == agent.id:
            raise HTTPException(status_code=400, detail="Cannot create escrow with yourself")

        mode = req.mode or "hub-held"

        if mode == "multisig":
            # Delegate to MultisigCoordinator
            try:
                result = _multisig.create(
                    db=db, buyer_id=agent.id, seller_id=seller.id,
                    amount=req.amount, description=req.description,
                    accept_timeout_hours=req.accept_timeout_hours,
                    delivery_timeout_hours=req.delivery_timeout_hours,
                    review_timeout_hours=req.review_timeout_hours,
                    buyer_tier=agent.tier.value,
                )
            except (LookupError, PermissionError, ValueError) as exc:
                _handle_service_error(exc)

            return {
                "escrow_id": result["escrow_deal_id"],
                "multisig_escrow_id": result["id"],
                "mode": "multisig",
                "state": result["state"],
                "fee_collected": result["fee_collected"],
                "funded_amount": result["funded_amount"],
                "timeout_at": result["timeout_at"],
            }

        # Default: hub-held mode
        try:
            milestones_data = None
            if req.milestones:
                milestones_data = [
                    {
                        "description": m.description,
                        "amount": m.amount,
                        "delivery_timeout_hours": m.delivery_timeout_hours,
                        "review_timeout_hours": m.review_timeout_hours,
                    }
                    for m in req.milestones
                ]
            result = _svc.create_escrow(
                db=db, buyer_id=agent.id, seller_id=seller.id,
                amount=req.amount, description=req.description,
                accept_timeout_hours=req.accept_timeout_hours,
                delivery_timeout_hours=req.delivery_timeout_hours,
                review_timeout_hours=req.review_timeout_hours,
                buyer_tier=agent.tier.value,
                milestones=milestones_data,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    background_tasks.add_task(
        queue_webhook, result["seller_id"], "escrow.created", {
            "escrow_id": result["escrow_id"],
            "amount": result["amount"],
            "description": result["description"],
            "accept_deadline": result["accept_deadline"],
            "buyer_agent_name": result["buyer_agent_name"],
        },
    )
    return {
        "escrow_id": result["escrow_id"],
        "status": result["status"],
        "amount": result["amount"],
        "seller_agent_name": result["seller_agent_name"],
        "description": result["description"],
        "accept_deadline": result["accept_deadline"],
        "created_at": result["created_at"],
    }


@router.post("/{escrow_id}/accept")
async def accept_escrow(
    escrow_id: UUID,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_current_agent),
):
    """Seller accepts the escrow deal."""
    with get_db() as db:
        try:
            result = _svc.accept_escrow(db=db, escrow_id=escrow_id, seller_id=agent.id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    background_tasks.add_task(
        queue_webhook, result["buyer_id"], "escrow.accepted", {
            "escrow_id": result["escrow_id"],
            "delivery_deadline": result["delivery_deadline"],
            "seller_agent_name": result["seller_agent_name"],
        },
    )
    return {
        "escrow_id": result["escrow_id"],
        "status": result["status"],
        "delivery_deadline": result["delivery_deadline"],
    }


@router.post("/{escrow_id}/deliver")
async def deliver_escrow(
    escrow_id: UUID,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_current_agent),
):
    """Seller marks the escrow as delivered."""
    with get_db() as db:
        try:
            result = _svc.deliver_escrow(db=db, escrow_id=escrow_id, seller_id=agent.id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    background_tasks.add_task(
        queue_webhook, result["buyer_id"], "escrow.delivered", {
            "escrow_id": result["escrow_id"],
            "review_deadline": result["review_deadline"],
            "seller_agent_name": result["seller_agent_name"],
        },
    )
    return {
        "escrow_id": result["escrow_id"],
        "status": result["status"],
        "review_deadline": result["review_deadline"],
    }


@router.post("/{escrow_id}/release")
async def release_escrow(
    escrow_id: UUID,
    req: EscrowReleaseRequest,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_current_agent),
):
    """Buyer releases funds to the seller (full or partial)."""
    with get_db() as db:
        try:
            result = _svc.release_escrow(
                db=db, escrow_id=escrow_id, buyer_id=agent.id,
                release_amount=req.release_amount,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    release_amt = result["release_amount"]
    fee_amt = result["fee_amount"]
    escrow_amt = result["amount"]
    seller_received = str(Decimal(release_amt) - Decimal(fee_amt))
    refunded = str(Decimal(escrow_amt) - Decimal(release_amt))

    background_tasks.add_task(
        queue_webhook, result["buyer_id"], "escrow.completed", {
            "escrow_id": result["escrow_id"],
            "released": release_amt,
            "refunded": refunded,
            "fee": fee_amt,
        },
    )
    background_tasks.add_task(
        queue_webhook, result["seller_id"], "escrow.completed", {
            "escrow_id": result["escrow_id"],
            "released": release_amt,
            "refunded": refunded,
            "fee": fee_amt,
        },
    )
    return {
        "escrow_id": result["escrow_id"],
        "status": result["status"],
        "released_to_seller": release_amt,
        "fee": fee_amt,
        "seller_received": seller_received,
        "refunded_to_buyer": refunded,
        "completed_at": result["completed_at"],
    }


@router.post("/{escrow_id}/cancel")
async def cancel_escrow(
    escrow_id: UUID,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_current_agent),
):
    """Buyer cancels the escrow (only while CREATED)."""
    with get_db() as db:
        try:
            result = _svc.cancel_escrow(db=db, escrow_id=escrow_id, buyer_id=agent.id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    background_tasks.add_task(
        queue_webhook, result["seller_id"], "escrow.cancelled", {
            "escrow_id": result["escrow_id"],
            "buyer_agent_name": result["buyer_agent_name"],
        },
    )
    return {
        "escrow_id": result["escrow_id"],
        "status": result["status"],
        "refunded": result["amount"],
    }


@router.post("/{escrow_id}/milestones/{milestone_seq}/deliver")
async def deliver_milestone(
    escrow_id: UUID,
    milestone_seq: int,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_current_agent),
):
    """Seller marks a specific milestone as delivered."""
    with get_db() as db:
        try:
            result = _svc.deliver_milestone(
                db=db, escrow_id=escrow_id,
                milestone_seq=milestone_seq, seller_id=agent.id,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    ms_status = result.get("status", "delivered")
    background_tasks.add_task(
        queue_webhook, result.get("buyer_id", ""), "escrow.milestone.delivered", {
            "escrow_id": result["escrow_id"],
            "milestone_sequence": result["milestone_sequence"],
            "milestone_status": ms_status,
            "review_deadline": result["review_deadline"],
        },
    )
    return {
        "escrow_id": result["escrow_id"],
        "milestone_sequence": result["milestone_sequence"],
        "milestone_status": ms_status,
        "review_deadline": result["review_deadline"],
    }


@router.post("/{escrow_id}/milestones/{milestone_seq}/release")
async def release_milestone(
    escrow_id: UUID,
    milestone_seq: int,
    req: MilestoneReleaseRequest,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_current_agent),
):
    """Buyer releases funds for a specific milestone."""
    with get_db() as db:
        try:
            result = _svc.release_milestone(
                db=db, escrow_id=escrow_id,
                milestone_seq=milestone_seq, buyer_id=agent.id,
                release_amount=req.release_amount,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    webhook_payload = {
        "escrow_id": result["escrow_id"],
        "milestone_sequence": result["milestone_sequence"],
        "released_to_seller": result["released_to_seller"],
        "fee": result["fee"],
        "deal_status": result["deal_status"],
    }
    background_tasks.add_task(
        queue_webhook, result.get("buyer_id", ""), "escrow.milestone.completed",
        webhook_payload,
    )
    background_tasks.add_task(
        queue_webhook, result.get("seller_id", ""), "escrow.milestone.completed",
        webhook_payload,
    )
    return result


@router.get("/{escrow_id}/milestones")
async def get_milestones(
    escrow_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Get milestones for a multi-milestone escrow."""
    with get_db() as db:
        try:
            milestones = _svc.get_milestones(
                db=db, escrow_id=escrow_id, agent_id=agent.id,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return {"milestones": milestones}


@router.get("/{escrow_id}")
async def get_escrow(
    escrow_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Get escrow details. Only buyer or seller may view."""
    with get_db() as db:
        try:
            result = _svc.get_escrow(db=db, escrow_id=escrow_id, agent_id=agent.id)
        except (LookupError, PermissionError) as exc:
            _handle_service_error(exc)
        # Include milestones for multi-milestone deals
        if result.get("is_multi_milestone"):
            try:
                milestones = _svc.get_milestones(
                    db=db, escrow_id=escrow_id, agent_id=agent.id,
                )
                result = {**result, "milestones": milestones}
            except (LookupError, ValueError):
                pass
    return result


@router.get("")
async def list_escrows(
    agent: Agent = Depends(get_current_agent),
    role: Optional[str] = Query(default=None, pattern=r"^(buyer|seller|all)$"),
    status: Optional[str] = Query(default=None, pattern=r"^(created|accepted|delivered|completed|expired|cancelled)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List escrow deals for the authenticated agent."""
    effective_role = None if role == "all" else role
    with get_db() as db:
        return _svc.list_escrows(
            db=db, agent_id=agent.id, role=effective_role,
            status=status, limit=limit, offset=offset,
        )
