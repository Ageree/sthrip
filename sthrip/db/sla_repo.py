"""
SLA Repository — data-access layer for SLATemplate and SLAContract records.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Tuple
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import desc

from . import models
from .models import SLAStatus
from ._repo_base import _MAX_QUERY_LIMIT


class SLATemplateRepository:
    """SLA template CRUD operations."""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        provider_id: UUID,
        name: str,
        service_description: str,
        deliverables: list,
        response_time_secs: int,
        delivery_time_secs: int,
        base_price: Decimal,
        currency: str = "XMR",
        penalty_percent: int = 10,
    ) -> models.SLATemplate:
        """Create a new SLA template."""
        tmpl = models.SLATemplate(
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
        self.db.add(tmpl)
        self.db.flush()
        return tmpl

    def get_by_id(self, template_id: UUID) -> Optional[models.SLATemplate]:
        """Get template by ID."""
        return self.db.query(models.SLATemplate).filter(
            models.SLATemplate.id == template_id
        ).first()

    def list_by_provider(
        self,
        provider_id: UUID,
        active_only: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[models.SLATemplate], int]:
        """List templates for a provider."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(models.SLATemplate).filter(
            models.SLATemplate.provider_id == provider_id
        )
        if active_only:
            query = query.filter(models.SLATemplate.is_active.is_(True))

        total = query.count()
        items = query.order_by(desc(models.SLATemplate.created_at)).offset(offset).limit(limit).all()
        return items, total

    def list_active(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[models.SLATemplate], int]:
        """List all active templates (public listing)."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(models.SLATemplate).filter(
            models.SLATemplate.is_active.is_(True)
        )
        total = query.count()
        items = query.order_by(desc(models.SLATemplate.created_at)).offset(offset).limit(limit).all()
        return items, total

    def deactivate(self, template_id: UUID) -> int:
        """Deactivate a template. Returns rows affected."""
        return self.db.query(models.SLATemplate).filter(
            models.SLATemplate.id == template_id,
            models.SLATemplate.is_active.is_(True),
        ).update({"is_active": False})


class SLAContractRepository:
    """SLA contract CRUD and state transitions."""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        provider_id: UUID,
        consumer_id: UUID,
        template_id: Optional[UUID],
        service_description: str,
        deliverables: list,
        response_time_secs: int,
        delivery_time_secs: int,
        price: Decimal,
        currency: str = "XMR",
        penalty_percent: int = 10,
        escrow_deal_id: Optional[UUID] = None,
    ) -> models.SLAContract:
        """Create a new SLA contract in PROPOSED state."""
        contract = models.SLAContract(
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
            state=SLAStatus.PROPOSED,
        )
        self.db.add(contract)
        self.db.flush()
        return contract

    def get_by_id(self, contract_id: UUID) -> Optional[models.SLAContract]:
        """Get contract by ID."""
        return self.db.query(models.SLAContract).filter(
            models.SLAContract.id == contract_id
        ).first()

    def get_by_id_for_update(self, contract_id: UUID) -> Optional[models.SLAContract]:
        """Get contract by ID with row-level lock."""
        is_sqlite = self.db.bind and self.db.bind.dialect.name == "sqlite"
        query = self.db.query(models.SLAContract).filter(
            models.SLAContract.id == contract_id
        )
        if not is_sqlite:
            query = query.with_for_update()
        return query.first()

    def accept(self, contract_id: UUID) -> int:
        """Transition proposed -> accepted."""
        return self.db.query(models.SLAContract).filter(
            models.SLAContract.id == contract_id,
            models.SLAContract.state == SLAStatus.PROPOSED,
        ).update({"state": SLAStatus.ACCEPTED})

    def activate(self, contract_id: UUID) -> int:
        """Transition accepted -> active, set started_at."""
        now = datetime.now(timezone.utc)
        return self.db.query(models.SLAContract).filter(
            models.SLAContract.id == contract_id,
            models.SLAContract.state == SLAStatus.ACCEPTED,
        ).update({"state": SLAStatus.ACTIVE, "started_at": now})

    def deliver(self, contract_id: UUID, result_hash: str) -> int:
        """Transition active -> delivered, record delivery time."""
        now = datetime.now(timezone.utc)
        contract = self.get_by_id(contract_id)
        if not contract or contract.state != SLAStatus.ACTIVE:
            return 0

        delivery_time_actual = None
        if contract.started_at:
            started = contract.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            delivery_time_actual = int((now - started).total_seconds())

        return self.db.query(models.SLAContract).filter(
            models.SLAContract.id == contract_id,
            models.SLAContract.state == SLAStatus.ACTIVE,
        ).update({
            "state": SLAStatus.DELIVERED,
            "delivered_at": now,
            "result_hash": result_hash,
            "delivery_time_actual": delivery_time_actual,
        })

    def complete(self, contract_id: UUID, sla_met: bool) -> int:
        """Transition delivered -> completed."""
        return self.db.query(models.SLAContract).filter(
            models.SLAContract.id == contract_id,
            models.SLAContract.state == SLAStatus.DELIVERED,
        ).update({"state": SLAStatus.COMPLETED, "sla_met": sla_met})

    def breach(self, contract_id: UUID) -> int:
        """Transition active -> breached."""
        return self.db.query(models.SLAContract).filter(
            models.SLAContract.id == contract_id,
            models.SLAContract.state == SLAStatus.ACTIVE,
        ).update({"state": SLAStatus.BREACHED, "sla_met": False})

    def dispute(self, contract_id: UUID) -> int:
        """Transition active/delivered -> disputed."""
        return self.db.query(models.SLAContract).filter(
            models.SLAContract.id == contract_id,
            models.SLAContract.state.in_([SLAStatus.ACTIVE, SLAStatus.DELIVERED]),
        ).update({"state": SLAStatus.DISPUTED})

    def list_by_agent(
        self,
        agent_id: UUID,
        role: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[models.SLAContract], int]:
        """List contracts where agent is provider or consumer."""
        from sqlalchemy import or_
        limit = min(limit, _MAX_QUERY_LIMIT)

        query = self.db.query(models.SLAContract)
        if role == "provider":
            query = query.filter(models.SLAContract.provider_id == agent_id)
        elif role == "consumer":
            query = query.filter(models.SLAContract.consumer_id == agent_id)
        else:
            query = query.filter(or_(
                models.SLAContract.provider_id == agent_id,
                models.SLAContract.consumer_id == agent_id,
            ))

        if state:
            query = query.filter(models.SLAContract.state == state)

        total = query.count()
        items = query.order_by(desc(models.SLAContract.created_at)).offset(offset).limit(limit).all()
        return items, total

    def get_active_past_deadline(self) -> List[models.SLAContract]:
        """Return active contracts that have exceeded their deadlines."""
        now = datetime.now(timezone.utc)
        results = []
        active = self.db.query(models.SLAContract).filter(
            models.SLAContract.state == SLAStatus.ACTIVE,
            models.SLAContract.started_at.isnot(None),
        ).all()

        for c in active:
            started = c.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = (now - started).total_seconds()
            if elapsed > c.delivery_time_secs or elapsed > c.response_time_secs:
                results.append(c)
        return results
