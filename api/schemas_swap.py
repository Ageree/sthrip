"""
Pydantic request/response schemas for the /v2/swap/* endpoints.
"""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class SwapQuoteRequest(BaseModel):
    """Request body for POST /v2/swap/quote."""

    from_currency: str
    from_amount: Decimal = Field(..., gt=0)
    to_currency: str = "XMR"


class SwapCreateRequest(BaseModel):
    """Request body for POST /v2/swap/create."""

    from_currency: str
    from_amount: Decimal = Field(..., gt=0, le=Decimal("100"))
    to_currency: str = "XMR"


class SwapClaimRequest(BaseModel):
    """Request body for POST /v2/swap/{swap_id}/claim."""

    htlc_secret: str = Field(..., min_length=64, max_length=64)


class SwapResponse(BaseModel):
    """Response schema for swap order details.

    All numeric fields are returned as strings for precision safety.

    Exchange provider fields (external_order_id, deposit_address, provider_name)
    are populated when ChangeNOW or SideShift successfully creates an order.
    deposit_address is the address where the user should send source funds.
    """

    swap_id: str
    from_agent_id: str
    from_currency: str
    from_amount: str
    to_currency: str
    to_amount: str
    exchange_rate: str
    fee_amount: str
    state: str
    htlc_hash: str
    btc_tx_hash: Optional[str] = None
    xmr_tx_hash: Optional[str] = None
    lock_expiry: Optional[str] = None
    created_at: Optional[str] = None
    # Exchange provider fields
    external_order_id: Optional[str] = None
    deposit_address: Optional[str] = None
    provider_name: Optional[str] = None
