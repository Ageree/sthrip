"""Pydantic request/response models for multi-party payment endpoints."""

from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class MultiPartyRecipientInput(BaseModel):
    """A single recipient in a multi-party payment request."""
    agent_name: str = Field(..., min_length=1, max_length=255)
    amount: Decimal = Field(..., gt=0)


class MultiPartyCreateRequest(BaseModel):
    """Create a multi-party payment."""
    recipients: List[MultiPartyRecipientInput] = Field(..., min_length=1)
    currency: str = Field(default="XMR", max_length=10)
    require_all_accept: bool = Field(default=True)
    accept_hours: int = Field(default=2, ge=1, le=168)

    @field_validator("recipients")
    @classmethod
    def validate_recipients(cls, v):
        names = [r.agent_name for r in v]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate recipients are not allowed")
        return v


class MultiPartyListParams(BaseModel):
    """Query params for listing multi-party payments."""
    role: Optional[str] = Field(default=None, pattern=r"^(sender|recipient)$")
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
