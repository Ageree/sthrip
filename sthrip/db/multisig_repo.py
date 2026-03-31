"""
MultisigEscrowRepository — data-access layer for MultisigEscrow records.
"""

from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from .models import MultisigEscrow, MultisigRound


_VALID_PARTICIPANTS = frozenset({"buyer", "seller", "hub"})
_PARTICIPANTS_PER_ROUND = 3


class MultisigEscrowRepository:
    """Data access for 2-of-3 multisig escrow records."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, **kwargs) -> MultisigEscrow:
        """Create a new MultisigEscrow record."""
        record = MultisigEscrow(**kwargs)
        self.db.add(record)
        self.db.flush()
        return record

    def get_by_id(self, escrow_id: UUID) -> Optional[MultisigEscrow]:
        """Get multisig escrow by its own ID."""
        return (
            self.db.query(MultisigEscrow)
            .filter(MultisigEscrow.id == escrow_id)
            .first()
        )

    def get_by_deal_id(self, deal_id: UUID) -> Optional[MultisigEscrow]:
        """Get multisig escrow by the linked EscrowDeal ID."""
        return (
            self.db.query(MultisigEscrow)
            .filter(MultisigEscrow.escrow_deal_id == deal_id)
            .first()
        )

    def get_by_id_for_update(self, escrow_id: UUID) -> Optional[MultisigEscrow]:
        """Get multisig escrow with row-level lock for state transitions."""
        is_sqlite = self.db.bind and self.db.bind.dialect.name == "sqlite"
        query = self.db.query(MultisigEscrow).filter(
            MultisigEscrow.id == escrow_id,
        )
        if not is_sqlite:
            query = query.with_for_update()
        return query.first()

    def add_round(
        self,
        multisig_escrow_id: UUID,
        round_number: int,
        participant: str,
        multisig_info: str,
    ) -> MultisigRound:
        """Store a key exchange round submission."""
        if participant not in _VALID_PARTICIPANTS:
            raise ValueError(
                f"participant must be one of {sorted(_VALID_PARTICIPANTS)}"
            )
        round_entry = MultisigRound(
            multisig_escrow_id=multisig_escrow_id,
            round_number=round_number,
            participant=participant,
            multisig_info=multisig_info,
        )
        self.db.add(round_entry)
        self.db.flush()
        return round_entry

    def count_round_submissions(
        self, multisig_escrow_id: UUID, round_number: int,
    ) -> int:
        """Count how many participants submitted data for a given round."""
        return (
            self.db.query(MultisigRound)
            .filter(
                MultisigRound.multisig_escrow_id == multisig_escrow_id,
                MultisigRound.round_number == round_number,
            )
            .count()
        )

    def get_rounds(
        self, multisig_escrow_id: UUID, round_number: Optional[int] = None,
    ) -> List[MultisigRound]:
        """Get round submissions, optionally filtered by round number."""
        query = self.db.query(MultisigRound).filter(
            MultisigRound.multisig_escrow_id == multisig_escrow_id,
        )
        if round_number is not None:
            query = query.filter(MultisigRound.round_number == round_number)
        return query.order_by(MultisigRound.round_number, MultisigRound.participant).all()

    def update_state(self, escrow_id: UUID, new_state: str) -> int:
        """Update the state of a multisig escrow. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return (
            self.db.query(MultisigEscrow)
            .filter(MultisigEscrow.id == escrow_id)
            .update({"state": new_state, "updated_at": now})
        )
