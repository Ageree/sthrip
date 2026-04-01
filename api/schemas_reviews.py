"""Pydantic request/response models for the Reviews API."""

from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field


class ReviewCreateRequest(BaseModel):
    transaction_id: str = Field(..., description="UUID of the transaction being reviewed")
    transaction_type: str = Field(
        ...,
        pattern=r"^(payment|escrow|sla)$",
        description="One of: payment, escrow, sla",
    )
    overall_rating: int = Field(..., ge=1, le=5, description="Overall rating 1-5")
    speed_rating: Optional[int] = Field(default=None, ge=1, le=5)
    quality_rating: Optional[int] = Field(default=None, ge=1, le=5)
    reliability_rating: Optional[int] = Field(default=None, ge=1, le=5)
    comment: Optional[str] = Field(default=None, max_length=2000)


class ReviewResponse(BaseModel):
    id: str
    reviewer_id: str
    reviewed_id: str
    transaction_id: str
    transaction_type: str
    overall_rating: int
    speed_rating: Optional[int] = None
    quality_rating: Optional[int] = None
    reliability_rating: Optional[int] = None
    comment: Optional[str] = None
    is_verified: bool
    created_at: Optional[str] = None


class ReviewListResponse(BaseModel):
    reviews: List[ReviewResponse]
    total: int
    limit: int
    offset: int


class RatingSummaryResponse(BaseModel):
    agent_id: str
    total_reviews: int
    avg_overall: str
    avg_speed: str
    avg_quality: str
    avg_reliability: str
    five_star_count: int
    one_star_count: int
    last_review_at: Optional[str] = None


class ReviewProofRequest(BaseModel):
    min_reviews: int = Field(..., ge=1, le=1000, description="Minimum number of reviews")
    min_avg: float = Field(..., ge=1.0, le=5.0, description="Minimum average overall rating")


class ReviewProofResponse(BaseModel):
    commitment: str
    proof: str
    min_reviews: int
    min_avg: str


class ReviewProofVerifyRequest(BaseModel):
    commitment: str
    proof: str
    min_reviews: int = Field(..., ge=1, le=1000)
    min_avg: float = Field(..., ge=1.0, le=5.0)


class ReviewProofVerifyResponse(BaseModel):
    valid: bool
