"""Balance endpoints: deposit, withdraw, balance check."""

import asyncio
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Header, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.db.repository import BalanceRepository, TransactionRepository
from sthrip.services.idempotency import get_idempotency_store
from sthrip.services.webhook_service import queue_webhook
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.metrics import balance_ops_total
from api.deps import get_current_agent
from api.schemas import DepositRequest, WithdrawRequest
from api.helpers import get_hub_mode, get_wallet_service

logger = logging.getLogger("sthrip")

router = APIRouter(prefix="/v2/balance", tags=["balance"])


@router.get("")
async def get_balance(agent: Agent = Depends(get_current_agent)):
    """Get agent's hub balance"""
    with get_db() as db:
        repo = BalanceRepository(db)
        balance = repo.get_or_create(agent.id)
        return {
            "available": float(balance.available or 0),
            "pending": float(balance.pending or 0),
            "total_deposited": float(balance.total_deposited or 0),
            "total_withdrawn": float(balance.total_withdrawn or 0),
            "deposit_address": balance.deposit_address,
            "token": "XMR",
        }


@router.post("/deposit")
async def deposit_balance(
    req: Optional[DepositRequest] = None,
    agent: Agent = Depends(get_current_agent),
    idempotency_key: Optional[str] = Header(None),
):
    """Deposit XMR to hub balance."""
    hub_mode = get_hub_mode()

    store = get_idempotency_store() if idempotency_key else None
    if idempotency_key:
        cached = store.try_reserve(str(agent.id), "deposit", idempotency_key)
        if cached is not None:
            return cached

    try:
        if hub_mode == "onchain":
            wallet_svc = get_wallet_service()
            deposit_address = await asyncio.to_thread(wallet_svc.get_or_create_deposit_address, agent.id)
            min_conf = int(os.getenv("MONERO_MIN_CONFIRMATIONS", "10"))
            network = os.getenv("MONERO_NETWORK", "stagenet")
            response = {
                "deposit_address": deposit_address,
                "token": "XMR",
                "network": network,
                "min_confirmations": min_conf,
                "message": f"Send XMR to this address. Balance will be credited after {min_conf} confirmations.",
            }
        else:
            if req is None or req.amount is None:
                raise HTTPException(status_code=422, detail="amount is required in ledger mode")
            amount = req.amount
            with get_db() as db:
                repo = BalanceRepository(db)
                balance = repo.deposit(agent.id, amount)
            response = {
                "status": "deposited",
                "amount": float(amount),
                "new_balance": float(balance.available),
                "token": "XMR",
            }

        balance_ops_total.labels("deposit", "XMR").inc()
        audit_log(
            "balance.deposit",
            agent_id=agent.id,
            request_method="POST",
            request_path="/v2/balance/deposit",
            details={"mode": hub_mode},
        )

        if idempotency_key:
            store.store_response(str(agent.id), "deposit", idempotency_key, response)

        return response
    except Exception:
        if idempotency_key:
            store.release(str(agent.id), "deposit", idempotency_key)
        raise


@router.post("/withdraw")
async def withdraw_balance(
    req: WithdrawRequest,
    agent: Agent = Depends(get_current_agent),
    idempotency_key: Optional[str] = Header(None),
):
    """Withdraw XMR from hub balance to external address."""
    hub_mode = get_hub_mode()

    store = get_idempotency_store() if idempotency_key else None
    if idempotency_key:
        cached = store.try_reserve(str(agent.id), "withdraw", idempotency_key)
        if cached is not None:
            return cached

    try:
        amount = req.amount

        # Deduct balance atomically (deduct() uses row lock internally)
        with get_db() as db:
            repo = BalanceRepository(db)
            try:
                repo.deduct(agent.id, amount)
            except ValueError:
                raise HTTPException(status_code=400, detail="Insufficient balance for this withdrawal")
            balance = repo.get_or_create(agent.id)
            balance.total_withdrawn = (balance.total_withdrawn or Decimal("0")) + amount

        if hub_mode == "onchain":
            wallet_svc = get_wallet_service()
            try:
                tx_result = await asyncio.to_thread(wallet_svc.send_withdrawal, req.address, amount)
            except Exception as e:
                # Rollback balance on RPC failure
                with get_db() as db:
                    repo = BalanceRepository(db)
                    repo.credit(agent.id, amount)
                    bal = repo.get_or_create(agent.id)
                    bal.total_withdrawn = (bal.total_withdrawn or Decimal("0")) - amount
                logger.error("Withdrawal RPC failed for agent=%s: %s", agent.id, e)
                raise HTTPException(status_code=502, detail="Withdrawal processing failed. Please try again later.")

            network = os.getenv("MONERO_NETWORK", "stagenet")
            with get_db() as db:
                tx_repo = TransactionRepository(db)
                tx_repo.create(
                    tx_hash=tx_result["tx_hash"],
                    network=network,
                    from_agent_id=agent.id,
                    to_agent_id=None,
                    amount=amount,
                    fee=tx_result.get("fee", Decimal("0")),
                    payment_type="hub_routing",
                    status="pending",
                )

            queue_webhook(str(agent.id), "payment.withdrawal_sent", {
                "tx_hash": tx_result["tx_hash"],
                "amount": float(amount),
                "to_address": req.address[:8] + "...",
            })

            response = {
                "status": "sent",
                "tx_hash": tx_result["tx_hash"],
                "amount": float(amount),
                "fee": float(tx_result.get("fee", 0)),
                "to_address": req.address,
                "remaining_balance": float(balance.available),
                "token": "XMR",
            }
        else:
            response = {
                "status": "withdrawn",
                "amount": float(amount),
                "to_address": req.address,
                "remaining_balance": float(balance.available),
                "token": "XMR",
            }

        balance_ops_total.labels("withdrawal", "XMR").inc()
        audit_log(
            "balance.withdraw",
            agent_id=agent.id,
            request_method="POST",
            request_path="/v2/balance/withdraw",
            details={"amount": float(amount), "to_address": req.address[:8] + "...", "mode": hub_mode},
        )

        if idempotency_key:
            store.store_response(str(agent.id), "withdraw", idempotency_key, response)

        return response
    except Exception:
        if idempotency_key:
            store.release(str(agent.id), "withdraw", idempotency_key)
        raise


@router.get("/deposits")
async def list_deposits(
    limit: int = Query(default=20, ge=1, le=100),
    agent: Agent = Depends(get_current_agent),
):
    """List deposit transactions for current agent."""
    with get_db() as db:
        tx_repo = TransactionRepository(db)
        txs = tx_repo.list_by_agent(agent.id, direction="in", limit=limit)
        deposits = [
            {
                "tx_hash": tx.tx_hash,
                "amount": float(tx.amount),
                "confirmations": tx.confirmations or 0,
                "status": tx.status.value if hasattr(tx.status, "value") else str(tx.status),
                "created_at": tx.created_at.isoformat() if tx.created_at else None,
            }
            for tx in txs
        ]
    return {"deposits": deposits}
