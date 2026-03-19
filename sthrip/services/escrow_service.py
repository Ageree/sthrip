"""
Hub-held escrow service.

Flow: CREATED -> ACCEPTED -> DELIVERED -> COMPLETED (or EXPIRED / CANCELLED).
Fee: 0.1% of released amount only.
Tier fee multipliers: premium 0.5x (50% discount), verified 0.75x (25% discount).
Partial release: buyer specifies release_amount in [0, escrow.amount].
"""

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import case, literal, update
from sqlalchemy.orm import Session

from sthrip.db.models import (
    Agent, AgentReputation, EscrowDeal, EscrowStatus,
    FeeCollection, FeeCollectionStatus,
)
from sthrip.db.repository import (
    AgentRepository, BalanceRepository, EscrowRepository,
)
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.webhook_service import queue_webhook

logger = logging.getLogger("sthrip.escrow")

_DEFAULT_FEE_PERCENT = Decimal("0.001")

# Fee multiplier per tier (NOT the discount itself).
# premium: pays 50% of base fee (50% discount)
# verified: pays 75% of base fee (25% discount)
_TIER_FEE_MULTIPLIERS = {
    "premium": Decimal("0.5"),
    "verified": Decimal("0.75"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _deal_to_dict(deal: EscrowDeal) -> dict:
    status_val = deal.status.value if hasattr(deal.status, "value") else deal.status
    buyer_name = deal.buyer.agent_name if deal.buyer else None
    seller_name = deal.seller.agent_name if deal.seller else None
    return {
        "escrow_id": str(deal.id),
        "deal_hash": deal.deal_hash,
        "buyer_id": str(deal.buyer_id),
        "seller_id": str(deal.seller_id),
        "buyer_agent_name": buyer_name,
        "seller_agent_name": seller_name,
        "amount": str(deal.amount),
        "token": deal.token,
        "description": deal.description,
        "fee_percent": str(deal.fee_percent),
        "fee_amount": str(deal.fee_amount),
        "release_amount": str(deal.release_amount) if deal.release_amount is not None else None,
        "status": status_val,
        "accept_timeout_hours": deal.accept_timeout_hours,
        "delivery_timeout_hours": deal.delivery_timeout_hours,
        "review_timeout_hours": deal.review_timeout_hours,
        "accept_deadline": _iso(deal.accept_deadline),
        "delivery_deadline": _iso(deal.delivery_deadline),
        "review_deadline": _iso(deal.review_deadline),
        "created_at": _iso(deal.created_at),
        "accepted_at": _iso(deal.accepted_at),
        "delivered_at": _iso(deal.delivered_at),
        "completed_at": _iso(deal.completed_at),
        "cancelled_at": _iso(deal.cancelled_at),
    }


def _refetch_as_dict(repo: EscrowRepository, deal_id: UUID) -> dict:
    """Re-fetch deal after state change and convert to dict. Raises on vanish."""
    deal = repo.get_by_id(deal_id)
    if deal is None:
        raise RuntimeError(f"Escrow {deal_id} vanished after state transition")
    return _deal_to_dict(deal)


def _compute_fee_percent(buyer_tier: str) -> Decimal:
    multiplier = _TIER_FEE_MULTIPLIERS.get(buyer_tier, Decimal("1"))
    return _DEFAULT_FEE_PERCENT * multiplier


def _generate_deal_hash(
    buyer_id: UUID, seller_id: UUID, amount: Decimal, timestamp: datetime,
) -> str:
    """Generate unique deal hash. Includes random salt to prevent collisions."""
    salt = secrets.token_hex(8)
    raw = f"{buyer_id}{seller_id}{amount}{timestamp.isoformat()}{salt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _adjust_trust(db: Session, agent_id: UUID, delta: int) -> None:
    if delta == 0:
        return
    aid = UUID(str(agent_id))
    new_expr = AgentReputation.trust_score + delta
    clamped = case(
        (new_expr < 0, literal(0)),
        (new_expr > 100, literal(100)),
        else_=new_expr,
    )
    db.execute(
        update(AgentReputation)
        .where(AgentReputation.agent_id == aid)
        .values(trust_score=clamped, calculated_at=_now())
    )


def _apply_completion_trust(
    db: Session, buyer_id: UUID, seller_id: UUID,
    release_amount: Decimal, escrow_amount: Decimal,
) -> None:
    if escrow_amount <= Decimal("0"):
        return
    if release_amount == escrow_amount:
        _adjust_trust(db, seller_id, 1)
        _adjust_trust(db, buyer_id, 1)
    elif release_amount == Decimal("0"):
        _adjust_trust(db, seller_id, -2)
    elif release_amount / escrow_amount > Decimal("0.5"):
        _adjust_trust(db, seller_id, 1)
    else:
        _adjust_trust(db, seller_id, -1)


def _record_fee(db: Session, deal_id: UUID, token: str, fee_amount: Decimal) -> None:
    if fee_amount <= Decimal("0"):
        return
    db.add(FeeCollection(
        source_type="escrow", source_id=deal_id,
        amount=fee_amount, token=token,
        status=FeeCollectionStatus.PENDING,
    ))


class EscrowService:
    """Hub-held escrow: funds locked from buyer balance, released on completion."""

    def create_escrow(
        self, db: Session, buyer_id: UUID, seller_id: UUID, amount: Decimal,
        description: str, accept_timeout_hours: int = 24,
        delivery_timeout_hours: int = 48, review_timeout_hours: int = 24,
        buyer_tier: str = "free",
    ) -> dict:
        if buyer_id == seller_id:
            raise ValueError("Buyer and seller must be different agents")
        if amount <= Decimal("0"):
            raise ValueError("Amount must be positive")

        seller = AgentRepository(db).get_by_id(seller_id)
        if not seller:
            raise LookupError("Seller not found")
        if not seller.is_active:
            raise ValueError("Seller is not active")

        BalanceRepository(db).deduct(buyer_id, amount, token="XMR")

        now = _now()
        deal = EscrowRepository(db).create(
            deal_hash=_generate_deal_hash(buyer_id, seller_id, amount, now),
            buyer_id=buyer_id, seller_id=seller_id, amount=amount,
            description=description,
            accept_timeout_hours=accept_timeout_hours,
            delivery_timeout_hours=delivery_timeout_hours,
            review_timeout_hours=review_timeout_hours,
            fee_percent=_compute_fee_percent(buyer_tier),
        )

        audit_log(
            action="escrow.created", agent_id=buyer_id,
            resource_type="escrow", resource_id=deal.id,
            details={"seller_id": str(seller_id), "amount": str(amount)}, db=db,
        )
        return _deal_to_dict(deal)

    def accept_escrow(self, db: Session, escrow_id: UUID, seller_id: UUID) -> dict:
        repo = EscrowRepository(db)
        deal = repo.get_by_id_for_update(escrow_id)
        if not deal:
            raise LookupError("Escrow not found")
        if deal.seller_id != seller_id:
            raise PermissionError("Only the seller can accept this escrow")
        if deal.status != EscrowStatus.CREATED:
            raise ValueError(f"Cannot accept escrow in '{deal.status.value}' state")
        if deal.accept_deadline and _now() > deal.accept_deadline:
            raise ValueError("Accept deadline has passed")

        # Re-check seller is still active
        seller = AgentRepository(db).get_by_id(seller_id)
        if seller and not seller.is_active:
            raise ValueError("Seller agent has been deactivated")

        repo.accept(escrow_id, int(deal.delivery_timeout_hours))
        audit_log(
            action="escrow.accepted", agent_id=seller_id,
            resource_type="escrow", resource_id=escrow_id, db=db,
        )
        return _refetch_as_dict(repo, escrow_id)

    def deliver_escrow(self, db: Session, escrow_id: UUID, seller_id: UUID) -> dict:
        repo = EscrowRepository(db)
        deal = repo.get_by_id_for_update(escrow_id)
        if not deal:
            raise LookupError("Escrow not found")
        if deal.seller_id != seller_id:
            raise PermissionError("Only the seller can mark delivery")
        if deal.status != EscrowStatus.ACCEPTED:
            raise ValueError(f"Cannot deliver escrow in '{deal.status.value}' state")

        repo.deliver(escrow_id, int(deal.review_timeout_hours))
        audit_log(
            action="escrow.delivered", agent_id=seller_id,
            resource_type="escrow", resource_id=escrow_id, db=db,
        )
        return _refetch_as_dict(repo, escrow_id)

    def release_escrow(
        self, db: Session, escrow_id: UUID, buyer_id: UUID, release_amount: Decimal,
    ) -> dict:
        repo = EscrowRepository(db)
        deal = repo.get_by_id_for_update(escrow_id)
        if not deal:
            raise LookupError("Escrow not found")
        if deal.buyer_id != buyer_id:
            raise PermissionError("Only the buyer can release escrow")
        if deal.status != EscrowStatus.DELIVERED:
            raise ValueError(f"Cannot release escrow in '{deal.status.value}' state")

        escrow_amount = Decimal(str(deal.amount))
        if release_amount < Decimal("0") or release_amount > escrow_amount:
            raise ValueError(f"Release amount must be between 0 and {escrow_amount}")

        fee_percent = Decimal(str(deal.fee_percent)) if deal.fee_percent else _DEFAULT_FEE_PERCENT
        fee_amount = release_amount * fee_percent
        seller_receives = release_amount - fee_amount
        refund_amount = escrow_amount - release_amount
        token = deal.token or "XMR"
        deal_buyer_id = UUID(str(deal.buyer_id))
        deal_seller_id = UUID(str(deal.seller_id))

        bal = BalanceRepository(db)
        if seller_receives > Decimal("0"):
            bal.credit(deal_seller_id, seller_receives, token=token)
        if refund_amount > Decimal("0"):
            bal.credit(deal_buyer_id, refund_amount, token=token)

        _record_fee(db, deal.id, token, fee_amount)
        repo.release(escrow_id, release_amount, fee_amount)
        _apply_completion_trust(db, deal_buyer_id, deal_seller_id, release_amount, escrow_amount)

        audit_log(
            action="escrow.completed", agent_id=buyer_id,
            resource_type="escrow", resource_id=escrow_id,
            details={
                "release_amount": str(release_amount),
                "fee_amount": str(fee_amount),
                "refund_amount": str(refund_amount),
            }, db=db,
        )
        return _refetch_as_dict(repo, escrow_id)

    def cancel_escrow(self, db: Session, escrow_id: UUID, buyer_id: UUID) -> dict:
        repo = EscrowRepository(db)
        deal = repo.get_by_id_for_update(escrow_id)
        if not deal:
            raise LookupError("Escrow not found")
        if deal.buyer_id != buyer_id:
            raise PermissionError("Only the buyer can cancel this escrow")
        if deal.status != EscrowStatus.CREATED:
            raise ValueError(f"Cannot cancel escrow in '{deal.status.value}' state")

        BalanceRepository(db).credit(UUID(str(deal.buyer_id)), Decimal(str(deal.amount)), token=deal.token or "XMR")
        repo.cancel(escrow_id)

        audit_log(
            action="escrow.cancelled", agent_id=buyer_id,
            resource_type="escrow", resource_id=escrow_id, db=db,
        )
        return _refetch_as_dict(repo, escrow_id)

    def get_escrow(self, db: Session, escrow_id: UUID, agent_id: UUID) -> dict:
        deal = EscrowRepository(db).get_by_id(escrow_id)
        if not deal:
            raise LookupError("Escrow not found")
        if deal.buyer_id != agent_id and deal.seller_id != agent_id:
            raise LookupError("Escrow not found")
        return _deal_to_dict(deal)

    def list_escrows(
        self, db: Session, agent_id: UUID, role: Optional[str] = None,
        status: Optional[str] = None, limit: int = 50, offset: int = 0,
    ) -> dict:
        # Validate status against enum to prevent invalid queries
        if status:
            try:
                EscrowStatus(status)
            except ValueError:
                raise ValueError(f"Invalid status: {status}")

        items, total = EscrowRepository(db).list_by_agent(
            agent_id=agent_id, role=role, status=status,
            limit=limit, offset=offset,
        )
        return {
            "items": [_deal_to_dict(d) for d in items],
            "total": total, "limit": limit, "offset": offset,
        }

    def resolve_expired(self, db: Session) -> int:
        """Resolve all escrows past their deadline. Returns count resolved."""
        repo = EscrowRepository(db)
        resolved = 0
        for deal in repo.get_pending_expiry():
            try:
                if self._resolve_single(db, deal, repo):
                    resolved += 1
            except Exception:
                logger.exception("Failed to resolve expired escrow %s", deal.id)
        return resolved

    def _resolve_single(
        self, db: Session, deal: EscrowDeal, repo: EscrowRepository,
    ) -> bool:
        """Resolve a single expired deal. Returns True if resolved."""
        # Lock the row before any mutations to prevent double-processing
        locked = repo.get_by_id_for_update(deal.id)
        if locked is None:
            return False
        # Re-check status under lock (may have been resolved by another instance)
        if locked.status == EscrowStatus.CREATED:
            return self._expire_not_accepted(db, locked, repo)
        elif locked.status == EscrowStatus.ACCEPTED:
            return self._expire_not_delivered(db, locked, repo)
        elif locked.status == EscrowStatus.DELIVERED:
            return self._auto_release_full(db, locked, repo)
        return False

    def _expire_not_accepted(
        self, db: Session, deal: EscrowDeal, repo: EscrowRepository,
    ) -> bool:
        """Accept timeout: refund buyer, no trust impact."""
        amount = Decimal(str(deal.amount))
        buyer_id = UUID(str(deal.buyer_id))
        seller_id = UUID(str(deal.seller_id))
        token = deal.token or "XMR"

        rows = repo.expire(deal.id)
        if rows == 0:
            return False  # Already resolved by another process

        BalanceRepository(db).credit(buyer_id, amount, token=token)
        result = _refetch_as_dict(repo, deal.id)
        queue_webhook(str(buyer_id), "escrow.expired", result)
        queue_webhook(str(seller_id), "escrow.expired", result)
        audit_log(
            action="escrow.expired", resource_type="escrow", resource_id=deal.id,
            details={"reason": "accept_timeout", "refund": str(amount)}, db=db,
        )
        return True

    def _expire_not_delivered(
        self, db: Session, deal: EscrowDeal, repo: EscrowRepository,
    ) -> bool:
        """Delivery timeout: refund buyer, seller gets -3 trust."""
        amount = Decimal(str(deal.amount))
        buyer_id = UUID(str(deal.buyer_id))
        seller_id = UUID(str(deal.seller_id))
        token = deal.token or "XMR"

        rows = repo.expire(deal.id)
        if rows == 0:
            return False

        BalanceRepository(db).credit(buyer_id, amount, token=token)
        _adjust_trust(db, seller_id, -3)
        result = _refetch_as_dict(repo, deal.id)
        queue_webhook(str(buyer_id), "escrow.expired", result)
        queue_webhook(str(seller_id), "escrow.expired", result)
        audit_log(
            action="escrow.expired", resource_type="escrow", resource_id=deal.id,
            details={"reason": "delivery_timeout", "refund": str(amount)}, db=db,
        )
        return True

    def _auto_release_full(
        self, db: Session, deal: EscrowDeal, repo: EscrowRepository,
    ) -> bool:
        """Review timeout: auto-release 100% to seller (status COMPLETED)."""
        amount = Decimal(str(deal.amount))
        fee_percent = Decimal(str(deal.fee_percent)) if deal.fee_percent else _DEFAULT_FEE_PERCENT
        fee_amount = amount * fee_percent
        seller_receives = amount - fee_amount
        buyer_id = UUID(str(deal.buyer_id))
        seller_id = UUID(str(deal.seller_id))
        token = deal.token or "XMR"

        rows = repo.release(deal.id, amount, fee_amount)
        if rows == 0:
            return False

        if seller_receives > Decimal("0"):
            BalanceRepository(db).credit(seller_id, seller_receives, token=token)
        _record_fee(db, deal.id, token, fee_amount)
        _apply_completion_trust(db, buyer_id, seller_id, amount, amount)

        result = _refetch_as_dict(repo, deal.id)
        queue_webhook(str(buyer_id), "escrow.completed", result)
        queue_webhook(str(seller_id), "escrow.completed", result)
        audit_log(
            action="escrow.auto_released", resource_type="escrow", resource_id=deal.id,
            details={
                "reason": "review_timeout",
                "auto_release": str(amount), "fee": str(fee_amount),
            }, db=db,
        )
        return True
