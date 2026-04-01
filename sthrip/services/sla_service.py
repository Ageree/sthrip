"""
SLA Service — business logic for SLATemplate and SLAContract lifecycle.

Flow: PROPOSED -> ACCEPTED -> ACTIVE -> DELIVERED -> COMPLETED (or BREACHED).
Fee: 1% flat, consistent with EscrowService.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.balance_repo import BalanceRepository
from sthrip.db.models import SLAContract
from sthrip.db.sla_repo import SLAContractRepository, SLATemplateRepository
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.escrow_service import EscrowService
from sthrip.services.webhook_service import queue_webhook

logger = logging.getLogger("sthrip.sla")

_DEFAULT_FEE_PERCENT = Decimal("0.01")  # 1% flat


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _contract_to_dict(contract: SLAContract) -> dict:
    """Convert an SLAContract ORM object to an immutable dict."""
    state_val = (
        contract.state.value
        if hasattr(contract.state, "value")
        else contract.state
    )
    return {
        "contract_id": str(contract.id),
        "provider_id": str(contract.provider_id),
        "consumer_id": str(contract.consumer_id),
        "template_id": str(contract.template_id) if contract.template_id else None,
        "service_description": contract.service_description,
        "deliverables": contract.deliverables,
        "response_time_secs": contract.response_time_secs,
        "delivery_time_secs": contract.delivery_time_secs,
        "price": str(contract.price),
        "currency": contract.currency,
        "penalty_percent": contract.penalty_percent,
        "state": state_val,
        "escrow_deal_id": str(contract.escrow_deal_id) if contract.escrow_deal_id else None,
        "started_at": _iso(contract.started_at),
        "delivered_at": _iso(contract.delivered_at),
        "response_time_actual": contract.response_time_actual,
        "delivery_time_actual": contract.delivery_time_actual,
        "sla_met": contract.sla_met,
        "result_hash": contract.result_hash,
        "created_at": _iso(contract.created_at),
    }


class SLAService:
    """Business logic for SLA templates and contracts."""

    # ------------------------------------------------------------------
    # Template operations
    # ------------------------------------------------------------------

    def create_template(
        self,
        db: Session,
        provider_id: UUID,
        name: str,
        service_description: str,
        deliverables: list,
        response_time_secs: int,
        delivery_time_secs: int,
        base_price: Decimal,
        currency: str = "XMR",
        penalty_percent: int = 10,
    ) -> dict:
        """Create a reusable SLA template for a provider."""
        repo = SLATemplateRepository(db)
        tmpl = repo.create(
            provider_id=provider_id,
            name=name,
            service_description=service_description,
            deliverables=deliverables,
            response_time_secs=response_time_secs,
            delivery_time_secs=delivery_time_secs,
            base_price=base_price,
            currency=currency,
            penalty_percent=penalty_percent,
        )
        return {
            "template_id": str(tmpl.id),
            "provider_id": str(tmpl.provider_id),
            "name": tmpl.name,
            "service_description": tmpl.service_description,
            "deliverables": tmpl.deliverables,
            "response_time_secs": tmpl.response_time_secs,
            "delivery_time_secs": tmpl.delivery_time_secs,
            "base_price": str(tmpl.base_price),
            "currency": tmpl.currency,
            "penalty_percent": tmpl.penalty_percent,
            "is_active": tmpl.is_active,
        }

    # ------------------------------------------------------------------
    # Contract lifecycle
    # ------------------------------------------------------------------

    def create_contract(
        self,
        db: Session,
        consumer_id: UUID,
        provider_id: UUID,
        name: str,
        service_description: str,
        deliverables: list,
        response_time_secs: int,
        delivery_time_secs: int,
        price: Decimal,
        currency: str = "XMR",
        penalty_percent: int = 10,
        template_id: Optional[UUID] = None,
    ) -> dict:
        """Create an SLA contract and a matching hub-held escrow deal.

        Raises:
            ValueError: consumer == provider, or consumer balance < price.
        """
        if consumer_id == provider_id:
            raise ValueError("Consumer and provider must be different agents")

        available = BalanceRepository(db).get_available(consumer_id, currency)
        if available < price:
            raise ValueError(
                f"Insufficient balance: consumer has {available} {currency}, "
                f"needs {price} {currency}"
            )

        escrow_result = EscrowService().create_escrow(
            db,
            buyer_id=consumer_id,
            seller_id=provider_id,
            amount=price,
            description=service_description,
        )
        escrow_deal_id = UUID(escrow_result["escrow_id"])

        repo = SLAContractRepository(db)
        contract = repo.create(
            provider_id=provider_id,
            consumer_id=consumer_id,
            template_id=template_id,
            service_description=service_description,
            deliverables=deliverables,
            response_time_secs=response_time_secs,
            delivery_time_secs=delivery_time_secs,
            price=price,
            currency=currency,
            penalty_percent=penalty_percent,
            escrow_deal_id=escrow_deal_id,
        )

        audit_log(
            action="sla.contract.created",
            agent_id=consumer_id,
            resource_type="sla_contract",
            resource_id=contract.id,
            details={
                "provider_id": str(provider_id),
                "price": str(price),
                "escrow_deal_id": str(escrow_deal_id),
            },
            db=db,
        )
        queue_webhook(str(consumer_id), "sla.contract.created", _contract_to_dict(contract))
        queue_webhook(str(provider_id), "sla.contract.created", _contract_to_dict(contract))

        return _contract_to_dict(contract)

    def accept_contract(
        self,
        db: Session,
        contract_id: UUID,
        provider_id: UUID,
    ) -> dict:
        """Provider accepts a proposed contract, activating it immediately.

        Transitions: PROPOSED -> ACCEPTED -> ACTIVE in a single call.

        Raises:
            LookupError: contract not found.
            PermissionError: caller is not the contract's provider.
        """
        repo = SLAContractRepository(db)
        contract = repo.get_by_id_for_update(contract_id)
        if contract is None:
            raise LookupError(f"SLA contract {contract_id} not found")
        if contract.provider_id != provider_id:
            raise PermissionError("Only the contract provider may accept")

        repo.accept(contract_id)
        repo.activate(contract_id)

        contract = repo.get_by_id(contract_id)
        result = _contract_to_dict(contract)

        audit_log(
            action="sla.contract.accepted",
            agent_id=provider_id,
            resource_type="sla_contract",
            resource_id=contract_id,
            details={"started_at": result["started_at"]},
            db=db,
        )
        queue_webhook(str(provider_id), "sla.contract.accepted", result)
        queue_webhook(str(contract.consumer_id), "sla.contract.accepted", result)

        return result

    def deliver_contract(
        self,
        db: Session,
        contract_id: UUID,
        provider_id: UUID,
        result_hash: str,
    ) -> dict:
        """Provider marks contract as delivered with a result hash.

        Raises:
            LookupError: contract not found.
            PermissionError: caller is not the contract's provider.
            ValueError: contract not in ACTIVE state.
        """
        repo = SLAContractRepository(db)
        contract = repo.get_by_id_for_update(contract_id)
        if contract is None:
            raise LookupError(f"SLA contract {contract_id} not found")
        if contract.provider_id != provider_id:
            raise PermissionError("Only the contract provider may deliver")

        rows = repo.deliver(contract_id, result_hash)
        if rows == 0:
            raise ValueError("Contract is not in ACTIVE state; cannot deliver")

        contract = repo.get_by_id(contract_id)
        result = _contract_to_dict(contract)

        audit_log(
            action="sla.contract.delivered",
            agent_id=provider_id,
            resource_type="sla_contract",
            resource_id=contract_id,
            details={"result_hash": result_hash},
            db=db,
        )
        queue_webhook(str(provider_id), "sla.contract.delivered", result)
        queue_webhook(str(contract.consumer_id), "sla.contract.delivered", result)

        return result

    def verify_contract(
        self,
        db: Session,
        contract_id: UUID,
        consumer_id: UUID,
    ) -> dict:
        """Consumer verifies delivery, completing the contract.

        Determines whether SLA was met by comparing delivery_time_actual
        against delivery_time_secs.

        Raises:
            LookupError: contract not found.
            PermissionError: caller is not the contract's consumer.
            ValueError: contract not in DELIVERED state.
        """
        repo = SLAContractRepository(db)
        contract = repo.get_by_id_for_update(contract_id)
        if contract is None:
            raise LookupError(f"SLA contract {contract_id} not found")
        if contract.consumer_id != consumer_id:
            raise PermissionError("Only the contract consumer may verify delivery")

        # Determine whether SLA was met
        sla_met: bool
        if contract.delivery_time_actual is not None:
            sla_met = contract.delivery_time_actual <= contract.delivery_time_secs
        else:
            # No timing data — treat as met (benefit of the doubt)
            sla_met = True

        rows = repo.complete(contract_id, sla_met=sla_met)
        if rows == 0:
            raise ValueError("Contract is not in DELIVERED state; cannot verify")

        contract = repo.get_by_id(contract_id)
        result = _contract_to_dict(contract)

        audit_log(
            action="sla.contract.completed",
            agent_id=consumer_id,
            resource_type="sla_contract",
            resource_id=contract_id,
            details={"sla_met": sla_met},
            db=db,
        )
        queue_webhook(str(consumer_id), "sla.contract.completed", result)
        queue_webhook(str(contract.provider_id), "sla.contract.completed", result)

        return result

    def get_template(self, db: Session, template_id: UUID) -> dict:
        """Return a single template by ID or raise LookupError."""
        repo = SLATemplateRepository(db)
        tmpl = repo.get_by_id(template_id)
        if tmpl is None:
            raise LookupError(f"SLA template {template_id} not found")
        return {
            "id": str(tmpl.id),
            "provider_id": str(tmpl.provider_id),
            "name": tmpl.name,
            "service_description": tmpl.service_description,
            "deliverables": tmpl.deliverables,
            "response_time_secs": tmpl.response_time_secs,
            "delivery_time_secs": tmpl.delivery_time_secs,
            "base_price": str(tmpl.base_price),
            "currency": tmpl.currency,
            "penalty_percent": tmpl.penalty_percent,
            "is_active": tmpl.is_active,
            "created_at": _iso(tmpl.created_at),
        }

    def list_templates(
        self,
        db: Session,
        provider_id: UUID,
        public: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List templates owned by provider, or all active templates when public=True."""
        repo = SLATemplateRepository(db)
        if public:
            items, total = repo.list_active(limit=limit, offset=offset)
        else:
            items, total = repo.list_by_provider(
                provider_id=provider_id, active_only=True,
                limit=limit, offset=offset,
            )
        serialized = [
            {
                "id": str(t.id),
                "provider_id": str(t.provider_id),
                "name": t.name,
                "service_description": t.service_description,
                "deliverables": t.deliverables,
                "response_time_secs": t.response_time_secs,
                "delivery_time_secs": t.delivery_time_secs,
                "base_price": str(t.base_price),
                "currency": t.currency,
                "penalty_percent": t.penalty_percent,
                "is_active": t.is_active,
                "created_at": _iso(t.created_at),
            }
            for t in items
        ]
        return {"items": serialized, "total": total, "limit": limit, "offset": offset}

    def get_contract(
        self,
        db: Session,
        contract_id: UUID,
        agent_id: UUID,
    ) -> dict:
        """Return a contract visible to agent_id or raise LookupError / PermissionError."""
        repo = SLAContractRepository(db)
        contract = repo.get_by_id(contract_id)
        if contract is None:
            raise LookupError(f"SLA contract {contract_id} not found")
        if contract.provider_id != agent_id and contract.consumer_id != agent_id:
            raise PermissionError("Access denied: not a party to this contract")
        return _contract_to_dict(contract)

    def list_contracts(
        self,
        db: Session,
        agent_id: UUID,
        role: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List contracts for an agent (optional role/state filters)."""
        repo = SLAContractRepository(db)
        items, total = repo.list_by_agent(
            agent_id=agent_id, role=role, state=state,
            limit=limit, offset=offset,
        )
        return {
            "items": [_contract_to_dict(c) for c in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def dispute_contract(
        self,
        db: Session,
        contract_id: UUID,
        agent_id: UUID,
    ) -> dict:
        """Either party can dispute an ACTIVE or DELIVERED contract.

        Raises:
            LookupError: contract not found.
            PermissionError: caller is not a party to the contract.
            ValueError: contract is not in a disputable state.
        """
        repo = SLAContractRepository(db)
        contract = repo.get_by_id_for_update(contract_id)
        if contract is None:
            raise LookupError(f"SLA contract {contract_id} not found")
        if contract.provider_id != agent_id and contract.consumer_id != agent_id:
            raise PermissionError("Only a party to the contract may raise a dispute")

        rows = repo.dispute(contract_id)
        if rows == 0:
            raise ValueError("Contract is not in ACTIVE or DELIVERED state; cannot dispute")

        contract = repo.get_by_id(contract_id)
        result = _contract_to_dict(contract)

        audit_log(
            action="sla.contract.disputed",
            agent_id=agent_id,
            resource_type="sla_contract",
            resource_id=contract_id,
            details={"disputed_by": str(agent_id)},
            db=db,
        )
        queue_webhook(str(contract.provider_id), "sla.contract.disputed", result)
        queue_webhook(str(contract.consumer_id), "sla.contract.disputed", result)

        return result

    def enforce_sla(self, db: Session) -> int:
        """Detect and breach all active contracts past their deadline.

        Returns:
            Number of contracts transitioned to BREACHED.
        """
        repo = SLAContractRepository(db)
        overdue = repo.get_active_past_deadline()
        count = 0
        for contract in overdue:
            rows = repo.breach(contract.id)
            if rows:
                count += 1
                audit_log(
                    action="sla.contract.breached",
                    agent_id=contract.provider_id,
                    resource_type="sla_contract",
                    resource_id=contract.id,
                    details={"auto_enforced": True},
                    db=db,
                )
                result = _contract_to_dict(repo.get_by_id(contract.id))
                queue_webhook(
                    str(contract.consumer_id), "sla.contract.breached", result
                )
                queue_webhook(
                    str(contract.provider_id), "sla.contract.breached", result
                )

        return count
