"""
Hub-held escrow service.

Flow: CREATED -> ACCEPTED -> DELIVERED -> COMPLETED (or EXPIRED / CANCELLED).
Fee: 1% flat on all releases. No tier discounts.
Partial release: buyer specifies release_amount in [0, escrow.amount].
"""

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy import case, literal, update
from sqlalchemy.orm import Session

from sthrip.db.models import (
    Agent, AgentReputation, EscrowDeal, EscrowMilestone, EscrowStatus,
    MilestoneStatus, FeeCollection, FeeCollectionStatus,
)
from sthrip.db.repository import (
    AgentRepository, BalanceRepository, EscrowRepository,
    MilestoneRepository,
)
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.webhook_service import queue_webhook

logger = logging.getLogger("sthrip.escrow")

_DEFAULT_FEE_PERCENT = Decimal("0.01")  # 1% flat, no tier discounts


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
        "is_multi_milestone": bool(deal.is_multi_milestone),
        "milestone_count": deal.milestone_count,
        "current_milestone": deal.current_milestone,
        "total_released": str(deal.total_released) if deal.total_released is not None else "0",
        "total_fees": str(deal.total_fees) if deal.total_fees is not None else "0",
    }


def _refetch_as_dict(repo: EscrowRepository, deal_id: UUID) -> dict:
    """Re-fetch deal after state change and convert to dict. Raises on vanish."""
    deal = repo.get_by_id(deal_id)
    if deal is None:
        raise RuntimeError(f"Escrow {deal_id} vanished after state transition")
    return _deal_to_dict(deal)


def _milestone_to_dict(ms: EscrowMilestone) -> dict:
    """Convert a milestone ORM object to an immutable dict."""
    status_val = ms.status.value if hasattr(ms.status, "value") else ms.status
    return {
        "sequence": ms.sequence,
        "description": ms.description,
        "amount": str(ms.amount),
        "status": status_val,
        "delivery_timeout_hours": ms.delivery_timeout_hours,
        "review_timeout_hours": ms.review_timeout_hours,
        "delivery_deadline": _iso(ms.delivery_deadline),
        "review_deadline": _iso(ms.review_deadline),
        "release_amount": str(ms.release_amount) if ms.release_amount is not None else None,
        "fee_amount": str(ms.fee_amount) if ms.fee_amount is not None else None,
        "activated_at": _iso(ms.activated_at),
        "delivered_at": _iso(ms.delivered_at),
        "completed_at": _iso(ms.completed_at),
    }


def _compute_fee_percent(buyer_tier: str) -> Decimal:
    """Flat 1% fee — tier parameter kept for backward compat but ignored."""
    return _DEFAULT_FEE_PERCENT


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
        milestones: Optional[List[dict]] = None,
    ) -> dict:
        if buyer_id == seller_id:
            raise ValueError("Buyer and seller must be different agents")
        if amount <= Decimal("0"):
            raise ValueError("Amount must be positive")

        # Validate milestone amounts if provided
        if milestones is not None:
            milestone_total = sum(
                Decimal(str(m["amount"])) for m in milestones
            )
            if milestone_total != amount:
                raise ValueError(
                    f"Sum of milestone amounts ({milestone_total}) must equal "
                    f"deal amount ({amount})"
                )

        seller = AgentRepository(db).get_by_id(seller_id)
        if not seller:
            raise LookupError("Seller not found")
        if not seller.is_active:
            raise ValueError("Seller is not active")

        BalanceRepository(db).deduct(buyer_id, amount, token="XMR")

        fee_percent = _compute_fee_percent(buyer_tier)
        now = _now()
        deal = EscrowRepository(db).create(
            deal_hash=_generate_deal_hash(buyer_id, seller_id, amount, now),
            buyer_id=buyer_id, seller_id=seller_id, amount=amount,
            description=description,
            accept_timeout_hours=accept_timeout_hours,
            delivery_timeout_hours=delivery_timeout_hours,
            review_timeout_hours=review_timeout_hours,
            fee_percent=fee_percent,
        )

        # Multi-milestone setup
        if milestones is not None:
            deal.is_multi_milestone = True
            deal.milestone_count = len(milestones)
            deal.current_milestone = 1
            deal.total_released = Decimal("0")
            deal.total_fees = Decimal("0")
            db.flush()

            MilestoneRepository(db).create_milestones(
                escrow_id=deal.id,
                milestones_data=milestones,
                fee_percent=fee_percent,
            )

        audit_log(
            action="escrow.created", agent_id=buyer_id,
            resource_type="escrow", resource_id=deal.id,
            details={
                "seller_id": str(seller_id),
                "amount": str(amount),
                "is_multi_milestone": milestones is not None,
                "milestone_count": len(milestones) if milestones else 0,
            }, db=db,
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

        # Activate milestone #1 for multi-milestone deals
        if deal.is_multi_milestone:
            ms_repo = MilestoneRepository(db)
            first_ms = ms_repo.get_by_escrow_and_sequence(escrow_id, 1)
            if first_ms and first_ms.status == MilestoneStatus.PENDING:
                ms_repo.activate(
                    first_ms.id, int(first_ms.delivery_timeout_hours),
                )

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
        if deal.is_multi_milestone:
            raise ValueError(
                "Use /milestones/{n}/deliver for multi-milestone escrows"
            )
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
        if deal.is_multi_milestone:
            raise ValueError(
                "Use /milestones/{n}/release for multi-milestone escrows"
            )
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

    # ── Multi-milestone methods ────────────────────────────────────────

    def get_milestones(
        self, db: Session, escrow_id: UUID, agent_id: UUID,
    ) -> List[dict]:
        """Get all milestones for an escrow. Returns list of dicts."""
        deal = EscrowRepository(db).get_by_id(escrow_id)
        if not deal:
            raise LookupError("Escrow not found")
        if deal.buyer_id != agent_id and deal.seller_id != agent_id:
            raise LookupError("Escrow not found")
        if not deal.is_multi_milestone:
            raise ValueError("This escrow does not have milestones")
        milestones = MilestoneRepository(db).get_by_escrow(escrow_id)
        return [_milestone_to_dict(m) for m in milestones]

    def deliver_milestone(
        self, db: Session, escrow_id: UUID, milestone_seq: int,
        seller_id: UUID,
    ) -> dict:
        """Mark a milestone as delivered by the seller.

        Transitions ACTIVE -> DELIVERED and sets review_deadline.
        """
        escrow_repo = EscrowRepository(db)
        deal = escrow_repo.get_by_id_for_update(escrow_id)
        if not deal:
            raise LookupError("Escrow not found")
        if not deal.is_multi_milestone:
            raise ValueError("This escrow does not have milestones")
        if deal.seller_id != seller_id:
            raise PermissionError("Only the seller can deliver milestones")
        if deal.status != EscrowStatus.ACCEPTED:
            raise ValueError(
                f"Cannot deliver milestone: deal is in '{deal.status.value}' state"
            )

        ms_repo = MilestoneRepository(db)
        ms = ms_repo.get_by_escrow_and_sequence_for_update(escrow_id, milestone_seq)
        if not ms:
            raise LookupError(f"Milestone #{milestone_seq} not found")
        if ms.status != MilestoneStatus.ACTIVE:
            raise ValueError(
                f"Cannot deliver milestone in '{ms.status.value}' state "
                f"(must be 'active')"
            )

        rows = ms_repo.deliver(ms.id, int(ms.review_timeout_hours))
        if rows == 0:
            raise ValueError("Milestone state transition failed (concurrent update)")

        audit_log(
            action="milestone.delivered", agent_id=seller_id,
            resource_type="escrow", resource_id=escrow_id,
            details={
                "milestone_sequence": milestone_seq,
                "milestone_id": str(ms.id),
            }, db=db,
        )

        # Re-fetch milestone for response
        ms_fresh = ms_repo.get_by_escrow_and_sequence(escrow_id, milestone_seq)
        result = _milestone_to_dict(ms_fresh)
        result["escrow_id"] = str(escrow_id)
        result["milestone_sequence"] = milestone_seq

        queue_webhook(str(deal.buyer_id), "milestone.delivered", result)
        queue_webhook(str(deal.seller_id), "milestone.delivered", result)
        return result

    def release_milestone(
        self, db: Session, escrow_id: UUID, milestone_seq: int,
        buyer_id: UUID, release_amount: Decimal,
    ) -> dict:
        """Release funds for a delivered milestone.

        Transitions DELIVERED -> COMPLETED, credits seller, refunds remainder,
        activates next milestone or completes the deal.
        """
        escrow_repo = EscrowRepository(db)
        deal = escrow_repo.get_by_id_for_update(escrow_id)
        if not deal:
            raise LookupError("Escrow not found")
        if not deal.is_multi_milestone:
            raise ValueError("This escrow does not have milestones")
        if deal.buyer_id != buyer_id:
            raise PermissionError("Only the buyer can release milestones")
        if deal.status != EscrowStatus.ACCEPTED:
            raise ValueError(
                f"Cannot release milestone: deal is in '{deal.status.value}' state"
            )

        ms_repo = MilestoneRepository(db)
        ms = ms_repo.get_by_escrow_and_sequence_for_update(escrow_id, milestone_seq)
        if not ms:
            raise LookupError(f"Milestone #{milestone_seq} not found")
        if ms.status != MilestoneStatus.DELIVERED:
            raise ValueError(
                f"Cannot release milestone in '{ms.status.value}' state "
                f"(must be 'delivered')"
            )

        milestone_amount = Decimal(str(ms.amount))
        if release_amount < Decimal("0") or release_amount > milestone_amount:
            raise ValueError(
                f"Release amount must be between 0 and {milestone_amount}"
            )

        fee_percent = Decimal(str(deal.fee_percent)) if deal.fee_percent else _DEFAULT_FEE_PERCENT
        fee_amount = release_amount * fee_percent
        seller_receives = release_amount - fee_amount
        refund_amount = milestone_amount - release_amount
        token = deal.token or "XMR"
        deal_buyer_id = UUID(str(deal.buyer_id))
        deal_seller_id = UUID(str(deal.seller_id))

        # Credit seller and refund buyer
        bal = BalanceRepository(db)
        if seller_receives > Decimal("0"):
            bal.credit(deal_seller_id, seller_receives, token=token)
        if refund_amount > Decimal("0"):
            bal.credit(deal_buyer_id, refund_amount, token=token)

        # Record fee
        _record_fee(db, deal.id, token, fee_amount)

        # Transition milestone to COMPLETED
        rows = ms_repo.release(ms.id, release_amount, fee_amount)
        if rows == 0:
            raise ValueError("Milestone state transition failed (concurrent update)")

        # Update deal-level totals
        prev_released = Decimal(str(deal.total_released or 0))
        prev_fees = Decimal(str(deal.total_fees or 0))
        new_released = prev_released + release_amount
        new_fees = prev_fees + fee_amount

        # Determine if this is the last milestone
        all_milestones = ms_repo.get_by_escrow(escrow_id)
        completed_count = sum(
            1 for m in all_milestones
            if (m.status == MilestoneStatus.COMPLETED
                or (m.id == ms.id))  # current one just transitioned
        )
        is_last = completed_count >= int(deal.milestone_count)

        if is_last:
            # Complete the deal
            escrow_amount = Decimal(str(deal.amount))
            db.query(EscrowDeal).filter(
                EscrowDeal.id == deal.id,
            ).update({
                "status": EscrowStatus.COMPLETED,
                "total_released": new_released,
                "total_fees": new_fees,
                "release_amount": new_released,
                "fee_amount": new_fees,
                "completed_at": _now(),
            })
            _apply_completion_trust(
                db, deal_buyer_id, deal_seller_id, new_released, escrow_amount,
            )
            # Cancel any remaining PENDING milestones (shouldn't be any, but safe)
            ms_repo.cancel_pending(escrow_id)
        else:
            # Update totals and activate next milestone
            next_seq = milestone_seq + 1
            db.query(EscrowDeal).filter(
                EscrowDeal.id == deal.id,
            ).update({
                "total_released": new_released,
                "total_fees": new_fees,
                "current_milestone": next_seq,
            })
            next_ms = ms_repo.get_by_escrow_and_sequence(escrow_id, next_seq)
            if next_ms and next_ms.status == MilestoneStatus.PENDING:
                ms_repo.activate(next_ms.id, int(next_ms.delivery_timeout_hours))

        audit_log(
            action="milestone.released", agent_id=buyer_id,
            resource_type="escrow", resource_id=escrow_id,
            details={
                "milestone_sequence": milestone_seq,
                "release_amount": str(release_amount),
                "fee_amount": str(fee_amount),
                "refund_amount": str(refund_amount),
                "is_last": is_last,
            }, db=db,
        )

        # Re-fetch for response
        deal_fresh = escrow_repo.get_by_id(escrow_id)
        deal_status = deal_fresh.status.value if hasattr(deal_fresh.status, "value") else deal_fresh.status
        result = {
            "escrow_id": str(escrow_id),
            "milestone_sequence": milestone_seq,
            "status": "completed",
            "released_to_seller": str(release_amount),
            "fee": str(fee_amount),
            "seller_received": str(seller_receives),
            "deal_status": deal_status,
            "deal_total_released": str(deal_fresh.total_released),
            "deal_total_fees": str(deal_fresh.total_fees),
        }

        event_type = "escrow.completed" if is_last else "milestone.released"
        queue_webhook(str(deal_buyer_id), event_type, result)
        queue_webhook(str(deal_seller_id), event_type, result)
        return result

    # ── Expiry resolution ────────────────────────────────────────────────

    def resolve_expired(self, db: Session) -> int:
        """Resolve all escrows past their deadline. Returns count resolved."""
        repo = EscrowRepository(db)
        resolved = 0

        # Deal-level expiry
        for deal in repo.get_pending_expiry():
            try:
                if self._resolve_single(db, deal, repo):
                    resolved += 1
            except Exception:
                logger.exception("Failed to resolve expired escrow %s", deal.id)

        # Milestone-level expiry
        ms_repo = MilestoneRepository(db)
        for ms in ms_repo.get_pending_milestone_expiry():
            try:
                if self._resolve_milestone_expiry(db, ms, repo, ms_repo):
                    resolved += 1
            except Exception:
                logger.exception(
                    "Failed to resolve expired milestone %s (escrow %s)",
                    ms.id, ms.escrow_id,
                )
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

    def _resolve_milestone_expiry(
        self, db: Session, ms: EscrowMilestone,
        escrow_repo: EscrowRepository, ms_repo: MilestoneRepository,
    ) -> bool:
        """Resolve a single expired milestone.

        ACTIVE past delivery_deadline -> EXPIRED, cancel remaining, refund buyer.
        DELIVERED past review_deadline -> auto-release 100%, activate next or complete.
        """
        deal = escrow_repo.get_by_id_for_update(ms.escrow_id)
        if deal is None:
            return False
        if deal.status != EscrowStatus.ACCEPTED:
            return False

        # Re-fetch milestone under parent lock
        locked_ms = ms_repo.get_by_escrow_and_sequence_for_update(
            ms.escrow_id, ms.sequence,
        )
        if locked_ms is None:
            return False

        if locked_ms.status == MilestoneStatus.ACTIVE:
            return self._expire_active_milestone(
                db, deal, locked_ms, escrow_repo, ms_repo,
            )
        elif locked_ms.status == MilestoneStatus.DELIVERED:
            return self._auto_release_milestone(
                db, deal, locked_ms, escrow_repo, ms_repo,
            )
        return False

    def _expire_active_milestone(
        self, db: Session, deal: EscrowDeal, ms: EscrowMilestone,
        escrow_repo: EscrowRepository, ms_repo: MilestoneRepository,
    ) -> bool:
        """Active milestone past delivery deadline: expire and refund.

        If milestone #1 -> deal EXPIRED. Otherwise -> PARTIALLY_COMPLETED.
        """
        rows = ms_repo.expire(ms.id)
        if rows == 0:
            return False

        buyer_id = UUID(str(deal.buyer_id))
        seller_id = UUID(str(deal.seller_id))
        token = deal.token or "XMR"

        # Cancel all remaining PENDING milestones
        ms_repo.cancel_pending(deal.id)

        # Calculate remaining (unreleased) amount to refund
        prev_released = Decimal(str(deal.total_released or 0))
        escrow_amount = Decimal(str(deal.amount))
        prev_fees = Decimal(str(deal.total_fees or 0))
        remaining = escrow_amount - prev_released - prev_fees
        if remaining > Decimal("0"):
            BalanceRepository(db).credit(buyer_id, remaining, token=token)

        # Determine deal end state
        is_first_milestone = ms.sequence == 1
        end_status = (
            EscrowStatus.EXPIRED if is_first_milestone
            else EscrowStatus.PARTIALLY_COMPLETED
        )

        now = _now()
        db.query(EscrowDeal).filter(EscrowDeal.id == deal.id).update({
            "status": end_status,
            "completed_at": now,
        })

        _adjust_trust(db, seller_id, -3)

        result = _refetch_as_dict(escrow_repo, deal.id)
        queue_webhook(str(buyer_id), "escrow.expired", result)
        queue_webhook(str(seller_id), "escrow.expired", result)
        audit_log(
            action="milestone.expired", resource_type="escrow", resource_id=deal.id,
            details={
                "reason": "delivery_timeout",
                "milestone_sequence": ms.sequence,
                "refund": str(remaining),
                "deal_status": end_status.value,
            }, db=db,
        )
        return True

    def _auto_release_milestone(
        self, db: Session, deal: EscrowDeal, ms: EscrowMilestone,
        escrow_repo: EscrowRepository, ms_repo: MilestoneRepository,
    ) -> bool:
        """Delivered milestone past review deadline: auto-release 100%.

        Activates next milestone or completes deal.
        """
        ms_amount = Decimal(str(ms.amount))
        fee_percent = Decimal(str(deal.fee_percent)) if deal.fee_percent else _DEFAULT_FEE_PERCENT
        fee_amount = ms_amount * fee_percent
        seller_receives = ms_amount - fee_amount
        buyer_id = UUID(str(deal.buyer_id))
        seller_id = UUID(str(deal.seller_id))
        token = deal.token or "XMR"

        rows = ms_repo.release(ms.id, ms_amount, fee_amount)
        if rows == 0:
            return False

        if seller_receives > Decimal("0"):
            BalanceRepository(db).credit(seller_id, seller_receives, token=token)
        _record_fee(db, deal.id, token, fee_amount)

        # Update deal totals
        prev_released = Decimal(str(deal.total_released or 0))
        prev_fees = Decimal(str(deal.total_fees or 0))
        new_released = prev_released + ms_amount
        new_fees = prev_fees + fee_amount

        # Check if last milestone
        all_milestones = ms_repo.get_by_escrow(deal.id)
        completed_count = sum(
            1 for m in all_milestones
            if (m.status == MilestoneStatus.COMPLETED
                or m.id == ms.id)
        )
        is_last = completed_count >= int(deal.milestone_count)

        if is_last:
            escrow_amount = Decimal(str(deal.amount))
            db.query(EscrowDeal).filter(EscrowDeal.id == deal.id).update({
                "status": EscrowStatus.COMPLETED,
                "total_released": new_released,
                "total_fees": new_fees,
                "release_amount": new_released,
                "fee_amount": new_fees,
                "completed_at": _now(),
            })
            _apply_completion_trust(db, buyer_id, seller_id, new_released, escrow_amount)
            ms_repo.cancel_pending(deal.id)

            result = _refetch_as_dict(escrow_repo, deal.id)
            queue_webhook(str(buyer_id), "escrow.completed", result)
            queue_webhook(str(seller_id), "escrow.completed", result)
        else:
            next_seq = ms.sequence + 1
            db.query(EscrowDeal).filter(EscrowDeal.id == deal.id).update({
                "total_released": new_released,
                "total_fees": new_fees,
                "current_milestone": next_seq,
            })
            next_ms = ms_repo.get_by_escrow_and_sequence(deal.id, next_seq)
            if next_ms and next_ms.status == MilestoneStatus.PENDING:
                ms_repo.activate(next_ms.id, int(next_ms.delivery_timeout_hours))

            result = _refetch_as_dict(escrow_repo, deal.id)
            queue_webhook(str(buyer_id), "milestone.auto_released", result)
            queue_webhook(str(seller_id), "milestone.auto_released", result)

        audit_log(
            action="milestone.auto_released", resource_type="escrow", resource_id=deal.id,
            details={
                "reason": "review_timeout",
                "milestone_sequence": ms.sequence,
                "auto_release": str(ms_amount),
                "fee": str(fee_amount),
                "is_last": is_last,
            }, db=db,
        )
        return True
