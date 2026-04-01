"""
Lending endpoints: credit scores, lending offers, and loans.

All endpoints require agent authentication via Bearer token.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.credit_service import CreditService
from api.deps import get_current_agent
from api.schemas_lending import CreateOfferRequest, LoanRequestBody

logger = logging.getLogger("sthrip")
router = APIRouter(prefix="/v2", tags=["lending"])

_svc = CreditService()


def _handle_service_error(exc: Exception) -> None:
    """Map service exceptions to HTTP responses."""
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


# ── Credit Score ─────────────────────────────────────────────────────────


@router.get("/me/credit-score")
def get_credit_score(agent: Agent = Depends(get_current_agent)):
    """Return the authenticated agent's credit score (cached or recalculated)."""
    with get_db() as db:
        try:
            return _svc.get_credit_score(db, agent.id)
        except (LookupError, ValueError) as exc:
            _handle_service_error(exc)


# ── Lending Offers ───────────────────────────────────────────────────────


@router.post("/lending/offers", status_code=201)
def create_offer(
    req: CreateOfferRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Create a new lending offer."""
    with get_db() as db:
        try:
            return _svc.create_offer(
                db,
                lender_id=agent.id,
                max_amount=req.max_amount,
                currency=req.currency,
                interest_rate_bps=req.interest_rate_bps,
                max_duration_secs=req.max_duration_secs,
                min_credit_score=req.min_credit_score,
                require_collateral=req.require_collateral,
                collateral_ratio=req.collateral_ratio,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


@router.get("/lending/offers")
def list_offers(
    currency: str = Query(default="XMR"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    agent: Agent = Depends(get_current_agent),
):
    """List active lending offers."""
    with get_db() as db:
        return _svc.list_offers(db, currency=currency, limit=limit, offset=offset)


@router.delete("/lending/offers/{offer_id}")
def withdraw_offer(
    offer_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Deactivate a lending offer (only the owning lender)."""
    with get_db() as db:
        try:
            return _svc.withdraw_offer(db, agent.id, offer_id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


# ── Loans ────────────────────────────────────────────────────────────────


@router.post("/loans/request", status_code=201)
def request_loan(
    req: LoanRequestBody,
    agent: Agent = Depends(get_current_agent),
):
    """Request a loan, matched against active offers."""
    with get_db() as db:
        try:
            return _svc.request_loan(
                db,
                borrower_id=agent.id,
                amount=req.amount,
                currency=req.currency,
                duration_secs=req.duration_secs,
                collateral_amount=req.collateral_amount,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


@router.post("/loans/{loan_id}/fund")
def fund_loan(
    loan_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Fund a requested loan (lender only)."""
    with get_db() as db:
        try:
            return _svc.fund_loan(db, agent.id, loan_id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


@router.post("/loans/{loan_id}/repay")
def repay_loan(
    loan_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Repay an active loan (borrower only)."""
    with get_db() as db:
        try:
            return _svc.repay_loan(db, agent.id, loan_id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


@router.get("/loans")
def list_loans(
    role: str = Query(default=None, pattern="^(lender|borrower)$"),
    state: str = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    agent: Agent = Depends(get_current_agent),
):
    """List loans for the authenticated agent."""
    with get_db() as db:
        return _svc.list_loans(
            db, agent_id=agent.id, role=role, state=state,
            limit=limit, offset=offset,
        )


@router.get("/loans/{loan_id}")
def get_loan(
    loan_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Get details of a specific loan (participants only)."""
    with get_db() as db:
        try:
            return _svc.get_loan(db, loan_id=loan_id, agent_id=agent.id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
