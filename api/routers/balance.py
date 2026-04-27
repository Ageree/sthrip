"""Balance endpoints: deposit, withdraw, balance check."""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Depends, Header, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.db.repository import BalanceRepository, TransactionRepository, PendingWithdrawalRepository
from sthrip.services.idempotency import get_idempotency_store
from sthrip.services.webhook_service import queue_webhook
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.metrics import balance_ops_total
from api.deps import get_current_agent
from api.schemas import DepositRequest, WithdrawRequest
from sthrip.config import get_settings
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
            "available": str(balance.available or 0),
            "pending": str(balance.pending or 0),
            "total_deposited": str(balance.total_deposited or 0),
            "total_withdrawn": str(balance.total_withdrawn or 0),
            "deposit_address": balance.deposit_address,
            "token": "XMR",
        }


@router.post("/deposit")
async def deposit_balance(
    req: Optional[DepositRequest] = None,
    agent: Agent = Depends(get_current_agent),
    idempotency_key: Optional[str] = Header(None, min_length=8, max_length=255),
):
    """Deposit XMR to hub balance."""
    import hashlib as _hashlib
    import json as _json

    hub_mode = get_hub_mode()
    store = get_idempotency_store() if idempotency_key else None

    _req_body = _json.dumps(
        req.dict() if req is not None else {},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    req_hash = _hashlib.sha256(_req_body.encode()).hexdigest() if idempotency_key else None

    try:
        if hub_mode == "onchain":
            # Onchain deposit: no balance mutation, just a wallet address lookup.
            # Fix 1: single try_reserve with a short-lived DB session for F-4 replay
            # detection. The DB write is still atomic because there is no concurrent
            # balance mutation here.
            if idempotency_key:
                with get_db() as db:
                    cached = store.try_reserve(
                        str(agent.id), "deposit", idempotency_key,
                        db=db, request_hash=req_hash,
                    )
                    if cached is not None:
                        return cached

            wallet_svc = get_wallet_service()
            rpc_timeout = get_settings().wallet_rpc_timeout * 3  # 3 retries max
            deposit_address = await asyncio.wait_for(
                asyncio.to_thread(wallet_svc.get_or_create_deposit_address, agent.id),
                timeout=rpc_timeout,
            )
            min_conf = get_settings().monero_min_confirmations
            network = get_settings().monero_network
            response = {
                "deposit_address": deposit_address,
                "token": "XMR",
                "network": network,
                "min_confirmations": min_conf,
                "message": f"Send XMR to this address. Balance will be credited after {min_conf} confirmations.",
            }

            if idempotency_key:
                with get_db() as db:
                    store.store_response(
                        str(agent.id), "deposit", idempotency_key, response,
                        db=db, request_hash=req_hash,
                    )
        else:
            if get_settings().environment not in ("dev",):
                raise HTTPException(
                    status_code=403,
                    detail="Ledger deposit is only available in dev environment",
                )
            if req is None or req.amount is None:
                raise HTTPException(status_code=422, detail="amount is required in ledger mode")
            amount = req.amount

            # Fix 1: single try_reserve inside the same DB session as the balance write.
            with get_db() as db:
                if idempotency_key:
                    cached = store.try_reserve(
                        str(agent.id), "deposit", idempotency_key,
                        db=db, request_hash=req_hash,
                    )
                    if cached is not None:
                        return cached

                repo = BalanceRepository(db)
                balance = repo.deposit(agent.id, amount)
                response = {
                    "status": "deposited",
                    "amount": str(amount),
                    "new_balance": str(balance.available),
                    "token": "XMR",
                }

                # Write idempotency row atomically with the balance change (Fix 2).
                if idempotency_key:
                    store.store_response(
                        str(agent.id), "deposit", idempotency_key, response,
                        db=db, request_hash=req_hash,
                    )

        balance_ops_total.labels(operation="deposit", token="XMR").inc()
        audit_log(
            "balance.deposit",
            agent_id=agent.id,
            request_method="POST",
            request_path="/v2/balance/deposit",
            details={
                "mode": hub_mode,
                "amount": str(req.amount) if req and req.amount else None,
            },
        )

        return response
    except Exception:
        if idempotency_key:
            store.release(str(agent.id), "deposit", idempotency_key)
        raise


def _deduct_and_create_pending(
    agent_id,
    amount,
    address,
    check_self_send: bool = False,
    idempotency_ctx: Optional[Dict[str, Any]] = None,
):
    """Atomically check self-send, deduct balance, create pending withdrawal,
    AND (when ``idempotency_ctx`` is supplied) write an in-progress idempotency
    row in the SAME DB transaction.

    The idempotency row is committed alongside the balance deduction so a
    replay can never see the side-effect without also seeing the idempotency
    row. This closes the F-4 reopen window Opus identified for the
    multi-session withdraw flow: previously the idempotency row was written in
    a separate session AFTER the wallet RPC, so a failure between balance
    deduction and ``store_response`` left the sentinel in Redis but no DB row
    — a retry treated the request as fresh and double-debited.

    ``idempotency_ctx`` keys: ``store``, ``agent_id``, ``endpoint``, ``key``,
    ``request_hash``. Provide an in-progress placeholder body the client can
    use to poll for final status. Caller updates the row on RPC success via
    ``store.store_response``.

    Returns pending_id.
    """
    with get_db() as db:
        repo = BalanceRepository(db)
        pw_repo = PendingWithdrawalRepository(db)

        if check_self_send:
            balance = repo.get_or_create(agent_id)
            if balance.deposit_address and balance.deposit_address == address:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot withdraw to your own deposit address (self-send)",
                )

        try:
            repo.withdraw(agent_id, amount)
        except ValueError:
            raise HTTPException(status_code=400, detail="Insufficient balance for this withdrawal")
        pending = pw_repo.create(agent_id=agent_id, amount=amount, address=address)

        if idempotency_ctx is not None:
            in_progress = {
                "status": "in_progress",
                "pending_id": str(pending.id),
                "amount": str(amount),
                "to_address": address[:8] + "...",
            }
            # 202 Accepted — body cached in same txn as the side-effect.
            idempotency_ctx["store"].store_response(
                idempotency_ctx["agent_id"],
                idempotency_ctx["endpoint"],
                idempotency_ctx["key"],
                in_progress,
                db=db,
                request_hash=idempotency_ctx.get("request_hash"),
                response_status=202,
            )
        return pending.id


async def _process_onchain_withdrawal(agent, amount, address, pending_id):
    """Execute onchain withdrawal via wallet RPC. Returns response dict."""
    wallet_svc = get_wallet_service()
    rpc_timeout = get_settings().wallet_rpc_timeout * 3  # 3 retries max
    try:
        tx_result = await asyncio.wait_for(
            asyncio.to_thread(wallet_svc.send_withdrawal, address, amount),
            timeout=rpc_timeout,
        )
    except asyncio.TimeoutError:
        # Treat same as RPC error — mark needs_review
        with get_db() as db:
            pw_repo = PendingWithdrawalRepository(db)
            pw_repo.mark_needs_review(
                pending_id,
                reason="RPC timeout — verify on-chain before refunding",
            )
        logger.error("Withdrawal RPC timed out for agent=%s pw=%s", agent.id, pending_id)
        raise HTTPException(
            status_code=504,
            detail="Withdrawal timed out. An admin will review this transaction.",
        )
    except Exception as e:
        # Mark as needs_review — do NOT auto-refund because the RPC may have
        # actually submitted the transaction (e.g. network timeout on response).
        # An admin must verify on-chain state before crediting back.
        with get_db() as db:
            pw_repo = PendingWithdrawalRepository(db)
            pw_repo.mark_needs_review(
                pending_id,
                reason=f"RPC error (see server logs): {type(e).__name__}",
            )
        logger.error("Withdrawal RPC failed for agent=%s pw=%s: %s", agent.id, pending_id, e)
        raise HTTPException(status_code=502, detail="Withdrawal processing failed. An admin will review this transaction.")

    network = get_settings().monero_network
    # Atomic: mark_completed + create transaction + fresh balance in one session
    with get_db() as db:
        pw_repo = PendingWithdrawalRepository(db)
        pw_repo.mark_completed(pending_id, tx_hash=tx_result["tx_hash"])
        tx_repo = TransactionRepository(db)
        tx_repo.create(
            tx_hash=tx_result["tx_hash"],
            network=network,
            from_agent_id=agent.id,
            to_agent_id=None,
            amount=amount,
            fee=tx_result.get("fee", Decimal("0")),
            payment_type="withdrawal",
            status="pending",
        )
        fresh_balance = BalanceRepository(db).get_or_create(agent.id)
        remaining = str(fresh_balance.available or 0)

    queue_webhook(str(agent.id), "payment.withdrawal_sent", {
        "tx_hash": tx_result["tx_hash"],
        "amount": str(amount),
        "to_address": address[:8] + "...",
    })

    return {
        "status": "sent",
        "tx_hash": tx_result["tx_hash"],
        "amount": str(amount),
        "fee": str(tx_result.get("fee", 0)),
        "to_address": address[:8] + "...",
        "remaining_balance": remaining,
        "token": "XMR",
    }


def _process_ledger_withdrawal(agent_id, pending_id):
    """Complete a ledger-mode withdrawal. Returns response dict with remaining balance."""
    with get_db() as db:
        pw_repo = PendingWithdrawalRepository(db)
        pw_repo.mark_completed(pending_id, tx_hash="ledger-mode")
        fresh_balance = BalanceRepository(db).get_or_create(agent_id)
        return str(fresh_balance.available or 0)


@router.post("/withdraw")
async def withdraw_balance(
    req: WithdrawRequest,
    agent: Agent = Depends(get_current_agent),
    idempotency_key: Optional[str] = Header(None, min_length=8, max_length=255),
):
    """Withdraw XMR from hub balance to external address."""
    import hashlib as _hashlib
    import json as _json

    hub_mode = get_hub_mode()
    store = get_idempotency_store() if idempotency_key else None

    _req_body = _json.dumps(req.dict(), sort_keys=True, separators=(",", ":"), default=str)
    req_hash = _hashlib.sha256(_req_body.encode()).hexdigest() if idempotency_key else None

    pending_id = None
    try:
        amount = req.amount

        # Single try_reserve with DB session for F-4 replay detection.
        if idempotency_key:
            with get_db() as db:
                cached = store.try_reserve(
                    str(agent.id), "withdraw", idempotency_key,
                    db=db, request_hash=req_hash,
                )
                if cached is not None:
                    return cached

        # F-4 v3 (Opus reopen fix): pass the idempotency context into
        # _deduct_and_create_pending so the idempotency row commits in the SAME
        # transaction as the balance debit + pending withdrawal. A replay can
        # never see balance debited without ALSO seeing an idempotency row.
        idem_ctx: Optional[Dict[str, Any]] = None
        if idempotency_key:
            idem_ctx = {
                "store": store,
                "agent_id": str(agent.id),
                "endpoint": "withdraw",
                "key": idempotency_key,
                "request_hash": req_hash,
            }
        pending_id = _deduct_and_create_pending(
            agent.id, amount, req.address,
            check_self_send=True,
            idempotency_ctx=idem_ctx,
        )

        if hub_mode == "onchain":
            response = await _process_onchain_withdrawal(agent, amount, req.address, pending_id)
        else:
            remaining = _process_ledger_withdrawal(agent.id, pending_id)
            response = {
                "status": "withdrawn",
                "amount": str(amount),
                "to_address": req.address[:8] + "...",
                "remaining_balance": remaining,
                "token": "XMR",
            }

        balance_ops_total.labels(operation="withdrawal", token="XMR").inc()
        audit_log(
            "balance.withdraw",
            agent_id=agent.id,
            request_method="POST",
            request_path="/v2/balance/withdraw",
            details={"amount": str(amount), "to_address": req.address[:8] + "...", "mode": hub_mode},
        )

        # Store idempotency response in a separate session after withdrawal completes
        # (Fix 1: single try_reserve; Fix 2: re-raises on non-race DB error).
        if idempotency_key:
            with get_db() as db:
                store.store_response(
                    str(agent.id), "withdraw", idempotency_key, response,
                    db=db, request_hash=req_hash,
                )

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
                "amount": str(tx.amount),
                "confirmations": tx.confirmations or 0,
                "status": tx.status.value if hasattr(tx.status, "value") else str(tx.status),
                "created_at": tx.created_at.isoformat() if tx.created_at else None,
            }
            for tx in txs
        ]
    return {"deposits": deposits}
