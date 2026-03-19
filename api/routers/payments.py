"""Payment endpoints: hub routing, history, lookup."""

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Header, BackgroundTasks, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent, HubRoute
from sthrip.db.repository import BalanceRepository, TransactionRepository
from sthrip.services.fee_collector import get_fee_collector
from sthrip.services.agent_registry import get_registry
from sthrip.services.idempotency import get_idempotency_store
from sthrip.services.webhook_service import queue_webhook
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.metrics import hub_payments_total
from api.deps import get_current_agent, get_fee_collector_dep, get_idempotency_store_dep
from api.schemas import HubPaymentRequest

logger = logging.getLogger("sthrip")

router = APIRouter(prefix="/v2/payments", tags=["payments"])



@router.post("/send")
async def send_payment():
    """Direct P2P not available in hub-only mode. Use /v2/payments/hub-routing instead."""
    raise HTTPException(status_code=501, detail="Direct P2P not available. Use /v2/payments/hub-routing for payments.")


def _validate_recipient(to_agent_name):
    """Look up and validate a recipient agent. Returns the recipient profile.

    NOTE: This opens its own DB session via the registry.  Prefer
    ``_validate_recipient_in_session`` inside an existing session to avoid
    race conditions between lookup and transfer.
    """
    registry = get_registry()
    recipient = registry.get_profile(to_agent_name)
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient agent not found")
    if not recipient.xmr_address:
        raise HTTPException(status_code=400, detail="Recipient has no XMR address configured")
    return recipient


def _validate_recipient_in_session(db, to_agent_name: str) -> Agent:
    """Validate recipient within the same DB session as the transfer.

    This ensures no race condition between lookup and deactivation.
    """
    agent = db.query(Agent).filter(
        Agent.agent_name == to_agent_name,
    ).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Recipient agent not found")
    if not agent.is_active:
        raise HTTPException(status_code=400, detail="Recipient agent is not active")
    if not agent.xmr_address:
        raise HTTPException(status_code=400, detail="Recipient has no XMR address configured")
    return agent


def _check_not_self_payment(sender_id, recipient_agent) -> None:
    """Reject self-payments: sender cannot be the recipient."""
    if str(sender_id) == str(recipient_agent.id):
        raise HTTPException(status_code=400, detail="Cannot send payment to yourself")


def _build_recipient_profile(agent: Agent):
    """Build a lightweight profile object from an Agent ORM model for response building.

    Returns a simple dataclass with the fields needed by ``_build_hub_payment_response``
    and ``_execute_hub_transfer``: id, agent_name, xmr_address, trust_score.
    """
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _RecipientProfile:
        id: str
        agent_name: str
        xmr_address: str
        trust_score: int

    reputation = getattr(agent, "reputation", None)
    trust_score = reputation.trust_score if reputation else 0

    return _RecipientProfile(
        id=str(agent.id),
        agent_name=agent.agent_name,
        xmr_address=agent.xmr_address,
        trust_score=trust_score,
    )


def _execute_hub_transfer(db, agent, recipient, amount, fee_info, req, idempotency_key, fee_collector=None):
    """Atomically deduct sender, credit recipient, create and confirm hub route.

    The duplicate check (via create_hub_route) runs BEFORE balance mutations
    to prevent double-credit on idempotent replay.
    """
    from uuid import UUID as _UUID

    collector = fee_collector or get_fee_collector()
    total_deduction = fee_info["total_deduction"]

    # Check for idempotent duplicate BEFORE any balance mutations
    route = collector.create_hub_route(
        from_agent_id=str(agent.id),
        to_agent_id=recipient.id,
        amount=amount,
        from_agent_tier=agent.tier.value,
        urgency=req.urgency,
        idempotency_key=idempotency_key,
        db=db,
    )

    if route.get("duplicate"):
        return route

    balance_repo = BalanceRepository(db)
    try:
        balance_repo.deduct(agent.id, total_deduction)
    except ValueError:
        raise HTTPException(status_code=400, detail="Insufficient balance")
    balance_repo.credit(_UUID(recipient.id), amount)

    collector.confirm_hub_route(route["payment_id"], db=db)

    return route


def _log_hub_payment(agent, req, route, fee_info, amount):
    """Log and audit a completed hub payment."""
    audit_log(
        "payment.hub_routing",
        agent_id=agent.id,
        request_method="POST",
        request_path="/v2/payments/hub-routing",
        details={
            "payment_id": route["payment_id"],
            "to_agent": req.to_agent_name,
            "amount": str(amount),
            "fee": str(fee_info["fee_amount"]),
        },
    )

    logger.info(json.dumps({
        "event": "hub_payment",
        "payment_id": route["payment_id"],
        "from_agent": agent.agent_name,
        "to_agent": req.to_agent_name,
        "amount": str(amount),
        "fee": str(fee_info["fee_amount"]),
        "urgency": req.urgency,
    }))


def _build_hub_payment_response(route, recipient, amount, fee_info, total_deduction):
    """Build the response dict for a completed hub payment."""
    return {
        "payment_id": route["payment_id"],
        "status": "confirmed",
        "payment_type": "hub_routing",
        "recipient": {
            "agent_name": recipient.agent_name,
            "address": recipient.xmr_address,
            "trust_score": recipient.trust_score,
        },
        "amount": str(amount),
        "fee": str(fee_info["fee_amount"]),
        "fee_percent": str(fee_info["fee_percent"]),
        "total_deducted": str(total_deduction),
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/hub-routing")
async def send_hub_routed_payment(
    req: HubPaymentRequest,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_current_agent),
    fee_collector=Depends(get_fee_collector_dep),
    idempotency_store=Depends(get_idempotency_store_dep),
    idempotency_key: Optional[str] = Header(None, min_length=8, max_length=255),
):
    """Send payment via hub routing"""
    store = idempotency_store if idempotency_key else None
    if idempotency_key:
        cached = store.try_reserve(str(agent.id), "hub-routing", idempotency_key)
        if cached is not None:
            return cached

    try:
        amount = req.amount

        with get_db() as db:
            # Validate recipient IN THE SAME session as the transfer to
            # prevent race between lookup and deactivation (HIGH-1 fix).
            recipient_agent = _validate_recipient_in_session(db, req.to_agent_name)
            _check_not_self_payment(agent.id, recipient_agent)
            recipient = _build_recipient_profile(recipient_agent)

            fee_info = fee_collector.calculate_hub_routing_fee(
                amount=amount,
                from_agent_tier=agent.tier.value,
                urgency=req.urgency,
            )
            route = _execute_hub_transfer(db, agent, recipient, amount, fee_info, req, idempotency_key, fee_collector=fee_collector)
        if route.get("duplicate"):
            return route

        background_tasks.add_task(
            queue_webhook, str(agent.id), "payment.sent",
            {"payment_id": route["payment_id"], "amount": str(amount),
             "to_agent": req.to_agent_name, "fee": str(fee_info["fee_amount"])},
        )

        _log_hub_payment(agent, req, route, fee_info, amount)

        response = _build_hub_payment_response(
            route, recipient, amount, fee_info, fee_info["total_deduction"],
        )

        hub_payments_total.labels(status="completed", tier=agent.tier.value).inc()

        if idempotency_key:
            store.store_response(str(agent.id), "hub-routing", idempotency_key, response)

        return response
    except Exception:
        if idempotency_key:
            store.release(str(agent.id), "hub-routing", idempotency_key)
        raise


# IMPORTANT: /history BEFORE /{payment_id} to avoid route shadowing
@router.get("/history")
async def get_payment_history(
    direction: Optional[str] = Query(default=None, pattern=r"^(in|out)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    agent: Agent = Depends(get_current_agent),
):
    """Get payment history"""
    with get_db() as db:
        repo = TransactionRepository(db)
        total = repo.count_by_agent(agent_id=agent.id, direction=direction)
        txs = repo.list_by_agent(
            agent_id=agent.id,
            direction=direction,
            limit=limit,
            offset=offset,
        )
        return {
            "items": [
                {
                    "tx_hash": tx.tx_hash,
                    "network": tx.network,
                    "amount": str(tx.amount),
                    "fee": str(tx.fee),
                    "fee_collected": str(tx.fee_collected),
                    "payment_type": tx.payment_type.value,
                    "status": tx.status.value,
                    "memo": tx.memo,
                    "created_at": tx.created_at.isoformat(),
                }
                for tx in txs
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@router.get("/{payment_id}")
async def get_payment(
    payment_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Look up a hub-routing payment by ID"""
    with get_db() as db:
        route = db.query(HubRoute).filter(HubRoute.payment_id == str(payment_id)).first()
        if not route:
            raise HTTPException(status_code=404, detail="Payment not found")
        if route.from_agent_id != agent.id and route.to_agent_id != agent.id:
            raise HTTPException(status_code=404, detail="Payment not found")
        return {
            "payment_id": route.payment_id,
            "from_agent_id": str(route.from_agent_id),
            "to_agent_id": str(route.to_agent_id),
            "amount": str(route.amount),
            "token": route.token,
            "fee_amount": str(route.fee_amount),
            "fee_percent": str(route.fee_percent) if route.fee_percent else None,
            "status": route.status.value,
            "created_at": route.created_at.isoformat() if route.created_at else None,
            "confirmed_at": route.confirmed_at.isoformat() if route.confirmed_at else None,
            "settled_at": route.settled_at.isoformat() if route.settled_at else None,
        }
