"""Pydantic request/response models for conditional and split payments."""

from decimal import Decimal
from typing import Dict, List, Optional, Any

from pydantic import BaseModel, Field, field_validator


class ConditionalPaymentCreate(BaseModel):
    """Request to create a conditional payment."""
    to_agent_name: str = Field(
        ..., min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9_-]+$",
    )
    amount: Decimal = Field(..., ge=Decimal("0.0001"), le=Decimal("10000"))
    currency: str = Field(default="XMR", pattern=r"^[A-Z]{2,10}$")
    condition_type: str = Field(
        ...,
        pattern=r"^(time_lock|escrow_completed|balance_threshold|webhook)$",
        description="One of: time_lock, escrow_completed, balance_threshold, webhook",
    )
    condition_config: Dict[str, Any] = Field(
        ..., description="Configuration for the condition type",
    )
    expires_hours: int = Field(default=24, ge=0, le=8760)
    memo: Optional[str] = Field(default=None, max_length=500)


class SplitRecipient(BaseModel):
    """A single recipient in a split payment."""
    agent_name: str = Field(
        ..., min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9_-]+$",
    )
    amount: Decimal = Field(..., gt=Decimal("0"), le=Decimal("10000"))


class SplitPaymentRequest(BaseModel):
    """Request to create a split payment to multiple recipients."""
    recipients: List[SplitRecipient] = Field(..., min_length=1, max_length=50)
    currency: str = Field(default="XMR", pattern=r"^[A-Z]{2,10}$")
    memo: Optional[str] = Field(default=None, max_length=500)
