"""Payment endpoints: hub routing, history, lookup."""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

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
from api.deps import get_current_agent
from api.schemas import HubPaymentRequest

logger = logging.getLogger("sthrip")

router = APIRouter(prefix="/v2/payments", tags=["payments"])

# Separate router for escrow (different prefix)
escrow_router = APIRouter(tags=["escrow"])


@escrow_router.post("/v2/escrow/create")
async def create_escrow():
    """Escrow is not available in this version"""
    raise HTTPException(status_code=501, detail="Escrow not available. Use hub routing for payments.")


@router.post("/send")
async def send_payment():
    """Direct P2P not available in hub-only mode. Use /v2/payments/hub-routing instead."""
    raise HTTPException(status_code=501, detail="Direct P2P not available. Use /v2/payments/hub-routing for payments.")


@router.post("/hub-routing")
async def send_hub_routed_payment(
    req: HubPaymentRequest,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_current_agent),
    idempotency_key: Optional[str] = Header(None),
):
    """Send payment via hub routing"""
    store = get_idempotency_store() if idempotency_key else None
    if idempotency_key:
        cached = store.try_reserve(str(agent.id), "hub-routing", idempotency_key)
        if cached is not None:
            return cached

    try:
        from decimal import Decimal
        from uuid import UUID as _UUID

        registry = get_registry()
        collector = get_fee_collector()

        recipient = registry.get_profile(req.to_agent_name)
        if not recipient:
            raise HTTPException(status_code=404, detail="Recipient agent not found")
        if not recipient.xmr_address:
            raise HTTPException(status_code=400, detail="Recipient has no XMR address configured")

        amount = req.amount
        fee_info = collector.calculate_hub_routing_fee(
            amount=amount,
            from_agent_tier=agent.tier.value,
            urgency=req.urgency,
        )
        total_deduction = fee_info["total_deduction"]

        # Atomic: deduct sender, credit recipient, create + confirm route in one transaction
        with get_db() as db:
            balance_repo = BalanceRepository(db)
            try:
                balance_repo.deduct(agent.id, total_deduction)
            except ValueError:
                raise HTTPException(status_code=400, detail="Insufficient balance")
            balance_repo.credit(_UUID(recipient.id), amount)

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

            collector.confirm_hub_route(route["payment_id"], db=db)

        background_tasks.add_task(
            queue_webhook,
            str(agent.id),
            "payment.sent",
            {
                "payment_id": route["payment_id"],
                "amount": float(amount),
                "to_agent": req.to_agent_name,
                "fee": float(fee_info["fee_amount"]),
            },
        )

        audit_log(
            "payment.hub_routing",
            agent_id=agent.id,
            request_method="POST",
            request_path="/v2/payments/hub-routing",
            details={
                "payment_id": route["payment_id"],
                "to_agent": req.to_agent_name,
                "amount": float(amount),
                "fee": float(fee_info["fee_amount"]),
            },
        )

        logger.info(json.dumps({
            "event": "hub_payment",
            "payment_id": route["payment_id"],
            "from_agent": agent.agent_name,
            "to_agent": req.to_agent_name,
            "amount": float(amount),
            "fee": float(fee_info["fee_amount"]),
            "urgency": req.urgency,
        }))

        response = {
            "payment_id": route["payment_id"],
            "status": "confirmed",
            "payment_type": "hub_routing",
            "recipient": {
                "agent_name": recipient.agent_name,
                "address": recipient.xmr_address,
                "trust_score": recipient.trust_score,
            },
            "amount": float(amount),
            "fee": float(fee_info["fee_amount"]),
            "fee_percent": float(fee_info["fee_percent"]),
            "total_deducted": float(total_deduction),
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }

        hub_payments_total.labels(agent.tier.value, req.urgency).inc()

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
    agent: Agent = Depends(get_current_agent),
):
    """Get payment history"""
    with get_db() as db:
        repo = TransactionRepository(db)
        txs = repo.list_by_agent(
            agent_id=agent.id,
            direction=direction,
            limit=limit,
        )
        return [
            {
                "tx_hash": tx.tx_hash,
                "network": tx.network,
                "amount": float(tx.amount),
                "fee": float(tx.fee),
                "fee_collected": float(tx.fee_collected),
                "payment_type": tx.payment_type.value,
                "status": tx.status.value,
                "memo": tx.memo,
                "created_at": tx.created_at.isoformat(),
            }
            for tx in txs
        ]


@router.get("/{payment_id}")
async def get_payment(
    payment_id: str,
    agent: Agent = Depends(get_current_agent),
):
    """Look up a hub-routing payment by ID"""
    with get_db() as db:
        route = db.query(HubRoute).filter(HubRoute.payment_id == payment_id).first()
        if not route:
            raise HTTPException(status_code=404, detail="Payment not found")
        if route.from_agent_id != agent.id and route.to_agent_id != agent.id:
            raise HTTPException(status_code=404, detail="Payment not found")
        return {
            "payment_id": route.payment_id,
            "from_agent_id": str(route.from_agent_id),
            "to_agent_id": str(route.to_agent_id),
            "amount": float(route.amount),
            "token": route.token,
            "fee_amount": float(route.fee_amount),
            "fee_percent": float(route.fee_percent) if route.fee_percent else None,
            "status": route.status.value,
            "created_at": route.created_at.isoformat() if route.created_at else None,
            "confirmed_at": route.confirmed_at.isoformat() if route.confirmed_at else None,
            "settled_at": route.settled_at.isoformat() if route.settled_at else None,
        }
