"""Payment endpoints: hub routing, history, lookup."""

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Header, BackgroundTasks, Query, Request

from sthrip.db.database import get_db
from sthrip.db.models import Agent, HubRoute, Transaction, PaymentType, TransactionStatus
from sthrip.db.repository import BalanceRepository, TransactionRepository
from sthrip.services.fee_collector import get_fee_collector
from sthrip.services.agent_registry import get_registry
from sthrip.services.idempotency import get_idempotency_store
from sthrip.services.webhook_service import queue_webhook
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.metrics import hub_payments_total
from sthrip.db.spending_policy_repo import SpendingPolicyRepository
from sthrip.services.spending_policy_service import SpendingPolicyService, PolicyViolation
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

    Phase 2 Sprint 2 cutover: commission deduction (0.3% Free / 0.1% Pro+) is
    handled atomically by ``TransactionRepository.create_with_commission``,
    which holds a SELECT FOR UPDATE on the sender balance, verifies funds,
    deducts ``amount + fee`` from sender, credits ``amount`` to receiver,
    inserts the Transaction row, and inserts the FeeCollection row — all in
    the same DB transaction.

    The legacy ``fee_collector.confirm_hub_route(...)`` path is intentionally
    NOT called here because it would insert a SECOND FeeCollection row at the
    legacy 1% rate (using the dict from ``calculate_hub_routing_fee``), which
    would mean users pay 1% + 0.3% (Free) or 1% + 0.1% (Pro+) — a regression.
    The HubRoute row is still created (admin dashboard + ``/payments/{id}``
    lookup depend on it) and its status is flipped inline to CONFIRMED.

    The duplicate check (via ``create_hub_route``) runs BEFORE balance
    mutations to prevent double-credit on idempotent replay.
    """
    from uuid import UUID as _UUID
    from sthrip.db.models import HubRouteStatus
    from sthrip.db.transaction_repo import (
        InsufficientBalanceError,
        TransactionRepository,
    )

    collector = fee_collector or get_fee_collector()

    # Idempotent duplicate check + HubRoute row insert. Note: this writes a
    # HubRoute row but does NOT touch agent balances or insert a FeeCollection
    # row at the legacy 1% rate — those side-effects only happen if we later
    # call confirm_hub_route, which we do NOT (commission path replaces it).
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

    # Convert XMR Decimal to integer piconero for the commission calculator.
    # Compute via the same Decimal->piconero path the wallet RPC uses.
    from decimal import Decimal as _Decimal
    amount_piconero = int(_Decimal(amount) * _Decimal(10**12))

    tx_repo = TransactionRepository(db)
    try:
        tx = tx_repo.create_with_commission(
            tx_hash=route["payment_id"],
            network="hub",
            from_agent_id=agent.id,
            to_agent_id=_UUID(recipient.id),
            amount_piconero=amount_piconero,
            payment_type=PaymentType.HUB_ROUTING.value,
            status=TransactionStatus.CONFIRMED.value,
            memo=getattr(req, "memo", None),
            idempotency_key=idempotency_key,
        )
    except InsufficientBalanceError:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    # Surface the *real* commission fee (in XMR) on the response by mutating
    # fee_info — the legacy ``calculate_hub_routing_fee`` figure is the OLD
    # 1% number which would mislead clients now that we charge the commission
    # rate. ``tx.fee`` is already stored in XMR Decimal scale by the repo.
    commission_fee_xmr = _Decimal(tx.fee or 0)
    fee_info["fee_amount"] = commission_fee_xmr
    # Only the rate stored on the Transaction matters now; keep fee_percent
    # as a string label so the admin/legacy clients still see something.
    fee_info["fee_percent"] = (
        _Decimal("0.003") if agent.tier.value == "free" else _Decimal("0.001")
    )
    fee_info["total_deduction"] = _Decimal(amount) + commission_fee_xmr

    # Flip HubRoute to CONFIRMED so admin dashboards and the /payments/{id}
    # lookup reflect the completed state. Inline so we don't trigger the
    # legacy FeeCollection insert in confirm_hub_route.
    from sthrip.db.models import HubRoute as _HubRoute
    is_sqlite = db.bind and db.bind.dialect.name == "sqlite"
    _q = db.query(_HubRoute).filter(_HubRoute.payment_id == route["payment_id"])
    if not is_sqlite:
        _q = _q.with_for_update()
    hub_row = _q.first()
    if hub_row is not None and hub_row.status == HubRouteStatus.PENDING:
        hub_row.status = HubRouteStatus.CONFIRMED
        hub_row.confirmed_at = datetime.now(timezone.utc)
        hub_row.fee_amount = commission_fee_xmr
        hub_row.fee_collected = True
        hub_row.fee_collected_at = datetime.now(timezone.utc)

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


def _get_redis_client(request: Request):
    """Extract the Redis client from the rate limiter, with fallback to None."""
    try:
        limiter = request.app.state.rate_limiter
        return getattr(limiter, "redis", None)
    except AttributeError:
        try:
            from sthrip.services.rate_limiter import get_rate_limiter
            limiter = get_rate_limiter()
            return getattr(limiter, "redis", None)
        except Exception:
            return None


@router.post("/hub-routing")
async def send_hub_routed_payment(
    req: HubPaymentRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_current_agent),
    fee_collector=Depends(get_fee_collector_dep),
    idempotency_store=Depends(get_idempotency_store_dep),
    idempotency_key: Optional[str] = Header(None, min_length=8, max_length=255),
    x_sthrip_session: Optional[str] = Header(None),
):
    """Send payment via hub routing"""
    import hashlib as _hashlib
    import json as _json

    store = idempotency_store if idempotency_key else None

    # Compute canonical request hash for body-mismatch detection (F-4).
    _req_body = _json.dumps(req.dict(), sort_keys=True, separators=(",", ":"), default=str)
    req_hash = _hashlib.sha256(_req_body.encode()).hexdigest() if idempotency_key else None

    try:
        amount = req.amount

        # Fix 1: open DB session FIRST, then call try_reserve exactly once with db=.
        # The previous two-call pattern (once without db, once inside with get_db)
        # caused try_reserve to set the sentinel on call 1, then see its own sentinel
        # on call 2 and raise 409 — blocking every first production POST with an
        # Idempotency-Key header.
        with get_db() as db:
            if idempotency_key:
                cached = store.try_reserve(
                    str(agent.id), "hub-routing", idempotency_key,
                    db=db, request_hash=req_hash,
                )
                if cached is not None:
                    return cached
            # --- Spending policy enforcement ---
            sp_repo = SpendingPolicyRepository(db)
            spending_policy = sp_repo.get_by_agent_id(agent.id)
            if spending_policy is not None:
                redis_client = _get_redis_client(request)
                policy_svc = SpendingPolicyService(redis_client=redis_client)
                session_id = x_sthrip_session or "default"
                try:
                    policy_svc.validate(
                        spending_policy,
                        amount,
                        req.to_agent_name,
                        session_id,
                    )
                except PolicyViolation as pv:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": "spending_policy_violation",
                            "field": pv.field,
                            "message": pv.message,
                        },
                    )

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

            response = _build_hub_payment_response(
                route, recipient, amount, fee_info, fee_info["total_deduction"],
            )

            # Write idempotency row INSIDE the same DB session as the payment
            # so the INSERT is atomic with the balance mutation (Fix 1 + Fix 2).
            # If _db_create raises (non-race DB error), the whole `with get_db()`
            # block rolls back — payment is NOT committed. Client retries safely.
            if idempotency_key:
                store.store_response(
                    str(agent.id), "hub-routing", idempotency_key, response,
                    db=db, request_hash=req_hash,
                )

        background_tasks.add_task(
            queue_webhook, str(agent.id), "payment.sent",
            {"payment_id": route["payment_id"], "amount": str(amount),
             "to_agent": req.to_agent_name, "fee": str(fee_info["fee_amount"])},
        )

        _log_hub_payment(agent, req, route, fee_info, amount)

        hub_payments_total.labels(status="completed", tier=agent.tier.value).inc()

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
    payment_id: str,
    agent: Agent = Depends(get_current_agent),
):
    """Look up a hub-routing payment by ID (accepts UUID or hub-route payment_id like hp_...)"""
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
            "amount": str(route.amount),
            "token": route.token,
            "fee_amount": str(route.fee_amount),
            "fee_percent": str(route.fee_percent) if route.fee_percent else None,
            "status": route.status.value,
            "created_at": route.created_at.isoformat() if route.created_at else None,
            "confirmed_at": route.confirmed_at.isoformat() if route.confirmed_at else None,
            "settled_at": route.settled_at.isoformat() if route.settled_at else None,
        }
