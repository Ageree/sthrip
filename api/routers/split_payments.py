"""Split payment endpoint: atomic multi-recipient payment."""

import logging

from fastapi import APIRouter, HTTPException, Depends

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.split_payment_service import SplitPaymentService
from api.deps import get_current_agent
from api.schemas_conditional import SplitPaymentRequest

logger = logging.getLogger("sthrip")
router = APIRouter(prefix="/v2/payments", tags=["payments"])


@router.post("/split", status_code=201)
async def split_payment(
    req: SplitPaymentRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Execute an atomic split payment to multiple recipients.

    All-or-nothing: if any recipient is not found or balance is insufficient,
    the entire transaction fails.
    """
    with get_db() as db:
        recipients = [
            {"agent_name": r.agent_name, "amount": r.amount}
            for r in req.recipients
        ]
        try:
            results = SplitPaymentService.pay_split(
                db=db,
                from_agent_id=agent.id,
                recipients=recipients,
                currency=req.currency,
                memo=req.memo,
            )
            return {"payments": results, "count": len(results)}
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
