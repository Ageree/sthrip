"""Pydantic request/response models for the Payment Channels API."""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class ChannelOpenRequest(BaseModel):
    agent_b_id: str = Field(..., description="UUID of the counterparty agent")
    deposit_a: Decimal = Field(..., ge=0, description="Deposit from agent A (caller)")
    deposit_b: Decimal = Field(default=Decimal("0"), ge=0, description="Deposit from agent B")
    settlement_period: int = Field(
        default=3600, ge=60, description="Settlement window in seconds"
    )


class ChannelStateUpdateRequest(BaseModel):
    nonce: int = Field(..., ge=1, description="Monotonically increasing update sequence")
    balance_a: Decimal = Field(..., ge=0, description="New balance for agent A")
    balance_b: Decimal = Field(..., ge=0, description="New balance for agent B")
    signature_a: str = Field(..., min_length=1, description="Signature from agent A")
    signature_b: str = Field(..., min_length=1, description="Signature from agent B")


class ChannelSettleRequest(BaseModel):
    nonce: int = Field(..., ge=0, description="Final state nonce")
    balance_a: Decimal = Field(..., ge=0, description="Final balance for agent A")
    balance_b: Decimal = Field(..., ge=0, description="Final balance for agent B")
    sig_a: str = Field(..., min_length=1, description="Signature from agent A")
    sig_b: str = Field(..., min_length=1, description="Signature from agent B")


class ChannelDisputeRequest(BaseModel):
    nonce: int = Field(..., ge=1, description="Higher-nonce state to replace current")
    balance_a: Decimal = Field(..., ge=0, description="Correct balance for agent A")
    balance_b: Decimal = Field(..., ge=0, description="Correct balance for agent B")
    sig_a: str = Field(..., min_length=1, description="Signature from agent A")
    sig_b: str = Field(..., min_length=1, description="Signature from agent B")


class ChannelResponse(BaseModel):
    channel_id: str
    channel_hash: str
    agent_a_id: str
    agent_b_id: str
    capacity: str
    deposit_a: str
    deposit_b: str
    balance_a: str
    balance_b: str
    nonce: int
    status: str
    settlement_period: int
    closes_at: Optional[str]
    settled_at: Optional[str]
    closed_at: Optional[str]
    created_at: Optional[str]
