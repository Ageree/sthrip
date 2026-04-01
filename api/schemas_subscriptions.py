"""Pydantic request/response models for the recurring subscriptions API."""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator


_VALID_INTERVALS = frozenset({"hourly", "daily", "weekly", "monthly"})


class SubscriptionCreateRequest(BaseModel):
    """Request body for POST /v2/subscriptions."""

    to_agent_name: str = Field(..., min_length=1, max_length=255)
    amount: Decimal = Field(..., gt=Decimal("0"), le=Decimal("10000"))
    interval: str
    max_payments: Optional[int] = Field(default=None, gt=0)

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, v: str) -> str:
        v_lower = v.lower()
        if v_lower not in _VALID_INTERVALS:
            raise ValueError(
                f"interval must be one of: {', '.join(sorted(_VALID_INTERVALS))}"
            )
        return v_lower


class SubscriptionUpdateRequest(BaseModel):
    """Request body for PATCH /v2/subscriptions/{id}."""

    amount: Optional[Decimal] = Field(default=None, gt=Decimal("0"), le=Decimal("10000"))
    interval: Optional[str] = None

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v_lower = v.lower()
        if v_lower not in _VALID_INTERVALS:
            raise ValueError(
                f"interval must be one of: {', '.join(sorted(_VALID_INTERVALS))}"
            )
        return v_lower


class SubscriptionResponse(BaseModel):
    """Response body for subscription endpoints (all fields as strings)."""

    id: str
    from_agent_id: str
    to_agent_id: str
    amount: str
    interval: str
    next_payment_at: Optional[str]
    last_payment_at: Optional[str]
    total_paid: str
    max_payments: Optional[int]
    payments_made: int
    is_active: bool
    created_at: Optional[str]
    cancelled_at: Optional[str]
