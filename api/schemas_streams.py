"""
Pydantic schemas for Payment Streams endpoints.
"""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class StreamStartRequest(BaseModel):
    """Request body for starting a new payment stream."""

    channel_id: str = Field(..., description="UUID of the payment channel")
    rate_per_second: Decimal = Field(
        ...,
        gt=Decimal("0"),
        description="Amount streamed per second (must be positive)",
    )


class StreamResponse(BaseModel):
    """Response for stream operations.

    All numeric/UUID fields are serialised as strings for cross-platform
    precision compatibility (same convention as escrow/channel responses).
    """

    stream_id: str
    channel_id: str
    from_agent_id: str
    to_agent_id: str
    rate_per_second: str
    state: str
    started_at: Optional[str] = None
    paused_at: Optional[str] = None
    stopped_at: Optional[str] = None
    total_streamed: str
    # Present on GET /{id} and after stop
    accrued: Optional[str] = None
