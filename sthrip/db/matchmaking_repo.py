"""
Matchmaking Repository — data-access layer for MatchRequest records.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import desc
from sqlalchemy.orm import Session

from . import models
from .models import MatchRequest, MatchRequestStatus
from ._repo_base import _MAX_QUERY_LIMIT


class MatchmakingRepository:
    """CRUD and state-transition operations for MatchRequest."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        requester_id: UUID,
        task_description: str,
        required_capabilities: list,
        budget: Decimal,
        currency: str,
        deadline_secs: int,
        min_rating: Decimal,
        auto_assign: bool,
        expires_at: datetime,
    ) -> MatchRequest:
        """Persist a new MatchRequest in SEARCHING state and return it."""
        req = MatchRequest(
            requester_id=requester_id,
            task_description=task_description,
            required_capabilities=required_capabilities,
            budget=budget,
            currency=currency,
            deadline_secs=deadline_secs,
            min_rating=min_rating,
            auto_assign=auto_assign,
            state=MatchRequestStatus.SEARCHING,
            expires_at=expires_at,
        )
        self.db.add(req)
        self.db.flush()
        return req

    def get_by_id(self, request_id: UUID) -> Optional[MatchRequest]:
        """Return a MatchRequest by primary key, or None."""
        return (
            self.db.query(MatchRequest)
            .filter(MatchRequest.id == request_id)
            .first()
        )

    def update_match(
        self,
        request_id: UUID,
        matched_agent_id: Optional[UUID],
        sla_contract_id: Optional[UUID],
        state: MatchRequestStatus,
    ) -> int:
        """Update matched_agent_id, sla_contract_id and state. Returns rows affected."""
        values = {"state": state}
        if matched_agent_id is not None:
            values["matched_agent_id"] = matched_agent_id
        if sla_contract_id is not None:
            values["sla_contract_id"] = sla_contract_id

        return (
            self.db.query(MatchRequest)
            .filter(MatchRequest.id == request_id)
            .update(values)
        )

    def list_by_requester(
        self,
        requester_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[MatchRequest], int]:
        """Return all requests for a requester with total count."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(MatchRequest).filter(
            MatchRequest.requester_id == requester_id
        )
        total = query.count()
        items = (
            query.order_by(desc(MatchRequest.created_at))
            .offset(offset)
            .limit(limit)
            .all()
        )
        return items, total

    def get_expired_searching(self) -> List[MatchRequest]:
        """Return all SEARCHING requests whose expires_at is in the past."""
        now = datetime.now(timezone.utc)
        return (
            self.db.query(MatchRequest)
            .filter(
                MatchRequest.state == MatchRequestStatus.SEARCHING,
                MatchRequest.expires_at < now,
            )
            .all()
        )
