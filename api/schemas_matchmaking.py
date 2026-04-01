"""Pydantic request/response models for the Matchmaking API endpoints."""

from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field


class MatchRequestCreate(BaseModel):
    task_description: str = Field(..., min_length=1, max_length=2000)
    required_capabilities: List[str] = Field(default_factory=list, max_length=20)
    budget: Decimal = Field(..., gt=0, le=10000)
    currency: str = Field(default="XMR", max_length=10)
    deadline_secs: int = Field(..., ge=1, le=2592000)  # max 30 days
    min_rating: Decimal = Field(default=Decimal("0"), ge=0, le=5)
    auto_assign: bool = Field(default=False)


class MatchRequestResponse(BaseModel):
    request_id: str
    requester_id: str
    task_description: str
    required_capabilities: List[str]
    budget: str
    currency: str
    deadline_secs: int
    min_rating: str
    auto_assign: bool
    matched_agent_id: Optional[str] = None
    sla_contract_id: Optional[str] = None
    state: str
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
