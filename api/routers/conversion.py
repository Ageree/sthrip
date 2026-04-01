"""Currency conversion endpoints.

Routes
------
POST /v2/balance/convert   — convert between supported currency pairs
GET  /v2/balance/all       — get all token balances for the agent
"""

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.conversion_service import ConversionService
from api.deps import get_current_agent
from api.schemas_conversion import ConversionRequest, ConversionResponse, MultiBalanceResponse

logger = logging.getLogger("sthrip")

router = APIRouter(prefix="/v2/balance", tags=["conversion"])

_svc = ConversionService()


@router.post("/convert", response_model=ConversionResponse)
async def convert_currency(
    req: ConversionRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Convert between supported currency pairs using hub balances."""
    with get_db() as db:
        try:
            result = _svc.convert(
                db,
                agent.id,
                req.from_currency,
                req.to_currency,
                req.amount,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.error("Conversion error for agent %s: %s", agent.id, exc)
            raise HTTPException(status_code=500, detail="Conversion failed")

        return ConversionResponse(
            from_currency=result["from_currency"],
            from_amount=result["from_amount"],
            to_currency=result["to_currency"],
            to_amount=result["net_to_amount"],
            rate=result["rate"],
            fee_amount=result["fee_amount"],
        )


@router.get("/all", response_model=MultiBalanceResponse)
async def get_all_balances(
    agent: Agent = Depends(get_current_agent),
):
    """Return all token balances held by the authenticated agent."""
    with get_db() as db:
        balances = _svc.get_all_balances(db, agent.id)
        return MultiBalanceResponse(balances=balances)
