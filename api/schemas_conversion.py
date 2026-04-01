"""
Pydantic schemas for currency conversion endpoints.
"""

from decimal import Decimal
from typing import Dict

from pydantic import BaseModel, Field


class ConversionRequest(BaseModel):
    from_currency: str
    to_currency: str
    amount: Decimal = Field(gt=0, le=100000)


class ConversionResponse(BaseModel):
    from_currency: str
    from_amount: str
    to_currency: str
    to_amount: str
    rate: str
    fee_amount: str


class MultiBalanceResponse(BaseModel):
    balances: Dict[str, str]
