"""
Pydantic request/response schemas for the /v2/lending/* and /v2/loans/* endpoints.
"""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class CreateOfferRequest(BaseModel):
    """Request body for POST /v2/lending/offers."""

    max_amount: Decimal = Field(..., gt=0)
    currency: str = "XMR"
    interest_rate_bps: int = Field(..., ge=0, le=10000)
    max_duration_secs: int = Field(..., gt=0)
    min_credit_score: int = Field(default=0, ge=0)
    require_collateral: bool = False
    collateral_ratio: int = Field(default=100, ge=0, le=500)


class LoanRequestBody(BaseModel):
    """Request body for POST /v2/loans/request."""

    amount: Decimal = Field(..., gt=0)
    currency: str = "XMR"
    duration_secs: int = Field(..., gt=0)
    collateral_amount: Decimal = Field(default=Decimal("0"), ge=0)
