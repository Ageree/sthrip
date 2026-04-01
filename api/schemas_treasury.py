"""Pydantic request/response models for Treasury Management endpoints."""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class TreasuryPolicyRequest(BaseModel):
    """Request body for PUT /v2/me/treasury/policy."""

    allocation: Dict[str, int] = Field(
        ...,
        description="Target allocation percentages by token. Values must sum to 100.",
    )
    rebalance_threshold_pct: int = Field(
        default=5, ge=1, le=50,
        description="Minimum drift percentage to trigger rebalance.",
    )
    cooldown_minutes: int = Field(
        default=60, ge=1, le=1440,
        description="Minimum minutes between rebalances.",
    )
    emergency_reserve_pct: int = Field(
        default=10, ge=0, le=50,
        description="Minimum percentage of total value to keep as XMR reserve.",
    )

    @model_validator(mode="after")
    def validate_allocation_sum(self) -> "TreasuryPolicyRequest":
        total = sum(self.allocation.values())
        if total != 100:
            raise ValueError(
                f"Allocation values must sum to 100, got {total}"
            )
        for token, pct in self.allocation.items():
            if pct < 0:
                raise ValueError(
                    f"Allocation for {token} must be non-negative, got {pct}"
                )
        return self


class TreasuryPolicyResponse(BaseModel):
    """Response body for treasury policy endpoints."""

    target_allocation: Dict[str, int]
    rebalance_threshold_pct: int
    cooldown_minutes: int
    emergency_reserve_pct: int
    is_active: bool
    last_rebalance_at: Optional[str] = None


class TreasuryStatusResponse(BaseModel):
    """Response body for GET /v2/me/treasury/status."""

    balances: Dict[str, str]
    allocation_pct: Dict[str, int]
    total_value_xusd: str


class TreasuryRebalanceResponse(BaseModel):
    """Response body for POST /v2/me/treasury/rebalance."""

    rebalanced: bool
    reason: Optional[str] = None
    conversions: List[dict] = Field(default_factory=list)
    pre_allocation: Optional[Dict[str, int]] = None
    post_allocation: Optional[Dict[str, int]] = None
    total_value_xusd: Optional[str] = None


class TreasuryHistoryItem(BaseModel):
    """A single rebalance log entry."""

    id: str
    trigger: str
    conversions: list
    pre_allocation: dict
    post_allocation: dict
    total_value_xusd: str
    created_at: Optional[str] = None


class TreasuryHistoryResponse(BaseModel):
    """Response body for GET /v2/me/treasury/history."""

    items: List[TreasuryHistoryItem]
