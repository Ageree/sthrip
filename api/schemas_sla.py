"""Pydantic request/response models for the SLA API endpoints."""

from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SLATemplateCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    service_description: str = Field(..., min_length=1, max_length=2000)
    deliverables: List[Dict[str, Any]] = Field(..., max_length=10)
    response_time_secs: int = Field(..., ge=1, le=86400)
    delivery_time_secs: int = Field(..., ge=1, le=604800)
    base_price: Decimal = Field(..., gt=0, le=10000)
    currency: str = Field(default="XMR")
    penalty_percent: int = Field(default=10, ge=0, le=50)


class SLATemplateResponse(BaseModel):
    id: str
    provider_id: str
    name: str
    service_description: str
    deliverables: List[Dict[str, Any]]
    response_time_secs: int
    delivery_time_secs: int
    base_price: str
    currency: str
    penalty_percent: int
    is_active: bool
    created_at: Optional[str] = None


class SLAContractCreateRequest(BaseModel):
    provider_agent_name: str = Field(..., min_length=1, max_length=255)
    template_id: Optional[str] = None
    service_description: Optional[str] = Field(default=None, max_length=2000)
    deliverables: Optional[List[Dict[str, Any]]] = None
    response_time_secs: Optional[int] = Field(default=None, ge=1, le=86400)
    delivery_time_secs: Optional[int] = Field(default=None, ge=1, le=604800)
    price: Decimal = Field(..., gt=0, le=10000)
    currency: str = Field(default="XMR")
    penalty_percent: Optional[int] = Field(default=None, ge=0, le=50)


class SLAContractResponse(BaseModel):
    contract_id: str
    provider_id: str
    consumer_id: str
    template_id: Optional[str] = None
    service_description: str
    deliverables: List[Dict[str, Any]]
    response_time_secs: int
    delivery_time_secs: int
    price: str
    currency: str
    penalty_percent: int
    state: str
    escrow_deal_id: Optional[str] = None
    started_at: Optional[str] = None
    delivered_at: Optional[str] = None
    response_time_actual: Optional[int] = None
    delivery_time_actual: Optional[int] = None
    sla_met: Optional[bool] = None
    result_hash: Optional[str] = None
    created_at: Optional[str] = None


class SLADeliverRequest(BaseModel):
    result_hash: str = Field(..., min_length=1, max_length=128)
