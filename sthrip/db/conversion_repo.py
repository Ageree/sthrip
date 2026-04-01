"""
ConversionRepository — data-access layer for CurrencyConversion records.

Follows the immutable-return pattern: create() returns a new ORM object;
callers receive a reference and must not mutate it outside the session.
"""

from decimal import Decimal
from typing import List, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from .models import CurrencyConversion


class ConversionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        agent_id: UUID,
        from_currency: str,
        from_amount: Decimal,
        to_currency: str,
        to_amount: Decimal,
        rate: Decimal,
        fee_amount: Decimal,
    ) -> CurrencyConversion:
        """Persist a new CurrencyConversion record and return it.

        Follows the repository pattern: callers supply all domain data;
        this method handles only persistence.
        """
        record = CurrencyConversion(
            agent_id=agent_id,
            from_currency=from_currency,
            from_amount=from_amount,
            to_currency=to_currency,
            to_amount=to_amount,
            rate=rate,
            fee_amount=fee_amount,
        )
        self.db.add(record)
        self.db.flush()
        return record

    def list_by_agent(
        self,
        agent_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[CurrencyConversion], int]:
        """Return a page of conversions for *agent_id* plus the total count.

        Returns
        -------
        (records, total)
            *records* is the requested page; *total* is the full count for
            the agent (useful for pagination).
        """
        base_query = self.db.query(CurrencyConversion).filter(
            CurrencyConversion.agent_id == agent_id
        )
        total = base_query.count()
        records = (
            base_query
            .order_by(CurrencyConversion.created_at.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        return records, total
