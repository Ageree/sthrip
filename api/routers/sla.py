"""SLA template and contract endpoints."""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.sla_service import SLAService
from api.deps import get_current_agent
from api.schemas_sla import (
    SLAContractCreateRequest,
    SLADeliverRequest,
    SLATemplateCreateRequest,
)

logger = logging.getLogger("sthrip")
router = APIRouter(prefix="/v2/sla", tags=["sla"])

_svc = SLAService()


def _handle_service_error(exc: Exception) -> None:
    """Map SLAService exceptions to HTTP responses."""
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


def _lookup_agent_by_name(db, name: str) -> Agent:
    """Resolve an agent by name, raising 404 if not found."""
    agent = db.query(Agent).filter(Agent.agent_name == name).first()
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return agent


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

@router.post("/templates", status_code=201)
async def create_template(
    req: SLATemplateCreateRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Create a reusable SLA template. The authenticated agent becomes the provider."""
    with get_db() as db:
        try:
            result = _svc.create_template(
                db=db,
                provider_id=agent.id,
                name=req.name,
                service_description=req.service_description,
                deliverables=req.deliverables,
                response_time_secs=req.response_time_secs,
                delivery_time_secs=req.delivery_time_secs,
                base_price=req.base_price,
                currency=req.currency,
                penalty_percent=req.penalty_percent,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    # Remap template_id -> id for the response schema
    return {**result, "id": result["template_id"]}


@router.get("/templates")
async def list_templates(
    agent: Agent = Depends(get_current_agent),
    public: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List SLA templates. `public=true` returns all active templates; default is my own."""
    with get_db() as db:
        return _svc.list_templates(
            db=db,
            provider_id=agent.id,
            public=public,
            limit=limit,
            offset=offset,
        )


@router.get("/templates/{template_id}")
async def get_template(
    template_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Get a single SLA template by ID."""
    with get_db() as db:
        try:
            return _svc.get_template(db=db, template_id=template_id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

@router.post("/contracts", status_code=201)
async def create_contract(
    req: SLAContractCreateRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Create an SLA contract. The authenticated agent becomes the consumer."""
    with get_db() as db:
        provider = _lookup_agent_by_name(db, req.provider_agent_name)
        if provider.id == agent.id:
            raise HTTPException(
                status_code=400, detail="Cannot create SLA contract with yourself"
            )

        # Resolve fields from template when template_id provided
        template_id = UUID(req.template_id) if req.template_id else None
        service_description = req.service_description or ""
        deliverables = req.deliverables or []
        response_time_secs = req.response_time_secs or 3600
        delivery_time_secs = req.delivery_time_secs or 86400
        penalty_percent = req.penalty_percent if req.penalty_percent is not None else 10

        try:
            result = _svc.create_contract(
                db=db,
                consumer_id=agent.id,
                provider_id=provider.id,
                name=service_description[:255] or "SLA Contract",
                service_description=service_description,
                deliverables=deliverables,
                response_time_secs=response_time_secs,
                delivery_time_secs=delivery_time_secs,
                price=req.price,
                currency=req.currency,
                penalty_percent=penalty_percent,
                template_id=template_id,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return result


@router.get("/contracts")
async def list_contracts(
    agent: Agent = Depends(get_current_agent),
    role: Optional[str] = Query(default=None, pattern=r"^(provider|consumer)$"),
    state: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List SLA contracts for the authenticated agent."""
    with get_db() as db:
        return _svc.list_contracts(
            db=db,
            agent_id=agent.id,
            role=role,
            state=state,
            limit=limit,
            offset=offset,
        )


@router.get("/contracts/{contract_id}")
async def get_contract(
    contract_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Get an SLA contract. Only the provider or consumer may view it."""
    with get_db() as db:
        try:
            return _svc.get_contract(db=db, contract_id=contract_id, agent_id=agent.id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


@router.patch("/contracts/{contract_id}/accept")
async def accept_contract(
    contract_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Provider accepts a proposed SLA contract, activating it immediately."""
    with get_db() as db:
        try:
            return _svc.accept_contract(db=db, contract_id=contract_id, provider_id=agent.id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


@router.patch("/contracts/{contract_id}/deliver")
async def deliver_contract(
    contract_id: UUID,
    req: SLADeliverRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Provider marks an SLA contract as delivered with a result hash."""
    with get_db() as db:
        try:
            return _svc.deliver_contract(
                db=db,
                contract_id=contract_id,
                provider_id=agent.id,
                result_hash=req.result_hash,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


@router.patch("/contracts/{contract_id}/verify")
async def verify_contract(
    contract_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Consumer verifies delivery and completes the SLA contract."""
    with get_db() as db:
        try:
            return _svc.verify_contract(db=db, contract_id=contract_id, consumer_id=agent.id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


@router.post("/contracts/{contract_id}/dispute")
async def dispute_contract(
    contract_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Either party raises a dispute on an ACTIVE or DELIVERED contract."""
    with get_db() as db:
        try:
            return _svc.dispute_contract(db=db, contract_id=contract_id, agent_id=agent.id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
