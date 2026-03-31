"""
MultisigCoordinator — orchestrates 2-of-3 Monero multisig escrow deals.

Fee: 1% collected UPFRONT before funds enter the multisig wallet.
States: setup_round_1 -> setup_round_2 -> setup_round_3 -> funded -> active
        -> releasing -> completed  (or cancelled / disputed)

Wallet RPC calls are stubbed — the coordinator records state transitions and
stores key-exchange data.  Actual RPC integration will be connected later.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.models import (
    Agent,
    EscrowDeal,
    EscrowStatus,
    FeeCollection,
    FeeCollectionStatus,
    MultisigEscrow,
    MultisigRound,
)
from sthrip.db.repository import (
    AgentRepository,
    BalanceRepository,
    EscrowRepository,
    MultisigEscrowRepository,
)
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.webhook_service import queue_webhook

logger = logging.getLogger("sthrip.multisig")

_FEE_PERCENT = Decimal("0.01")  # 1% upfront
_SETUP_TIMEOUT_HOURS = 24  # timeout for multisig setup rounds
_PARTICIPANTS_PER_ROUND = 3
_VALID_PARTICIPANTS = frozenset({"buyer", "seller", "hub"})

# State machine transitions
_ROUND_STATE_MAP = {
    1: ("setup_round_1", "setup_round_2"),
    2: ("setup_round_2", "setup_round_3"),
    3: ("setup_round_3", "funded"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _generate_deal_hash(
    buyer_id: UUID, seller_id: UUID, amount: Decimal, timestamp: datetime,
) -> str:
    """Generate unique deal hash with random salt."""
    salt = secrets.token_hex(8)
    raw = f"multisig:{buyer_id}{seller_id}{amount}{timestamp.isoformat()}{salt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _multisig_to_dict(ms: MultisigEscrow) -> dict:
    """Convert a MultisigEscrow ORM object to an immutable dict."""
    return {
        "id": str(ms.id),
        "escrow_deal_id": str(ms.escrow_deal_id),
        "multisig_address": ms.multisig_address,
        "state": ms.state,
        "fee_collected": str(ms.fee_collected),
        "funded_amount": str(ms.funded_amount) if ms.funded_amount is not None else None,
        "funded_tx_hash": ms.funded_tx_hash,
        "timeout_at": _iso(ms.timeout_at),
        "created_at": _iso(ms.created_at),
        "updated_at": _iso(ms.updated_at),
        "buyer_wallet_id": ms.buyer_wallet_id,
        "seller_wallet_id": ms.seller_wallet_id,
        "hub_wallet_id": ms.hub_wallet_id,
        "release_initiator": ms.release_initiator,
        "dispute_reason": ms.dispute_reason,
        "disputed_by": ms.disputed_by,
    }


class MultisigCoordinator:
    """Orchestrates 2-of-3 Monero multisig escrow creation and lifecycle."""

    def __init__(self, wallet_rpc=None) -> None:
        self._wallet = wallet_rpc
        self._fee_percent = _FEE_PERCENT

    def create(
        self,
        db: Session,
        buyer_id: UUID,
        seller_id: UUID,
        amount: Decimal,
        description: str = "",
        accept_timeout_hours: int = 24,
        delivery_timeout_hours: int = 48,
        review_timeout_hours: int = 24,
        buyer_tier: str = "free",
    ) -> dict:
        """Create a multisig escrow deal.

        Collects 1% fee upfront.  The remaining amount will enter the
        2-of-3 multisig wallet once setup rounds complete.
        """
        if buyer_id == seller_id:
            raise ValueError("Buyer and seller must be different agents")
        if amount <= Decimal("0"):
            raise ValueError("Amount must be positive")

        seller = AgentRepository(db).get_by_id(seller_id)
        if not seller:
            raise LookupError("Seller not found")
        if not seller.is_active:
            raise ValueError("Seller is not active")

        # Calculate fee and funded amount
        fee = (amount * self._fee_percent).quantize(Decimal("0.00000001"))
        funded_amount = amount - fee

        # Deduct full amount from buyer (fee + escrow funds)
        BalanceRepository(db).deduct(buyer_id, amount, token="XMR")

        # Create the underlying EscrowDeal record (links to existing system)
        now = _now()
        escrow_repo = EscrowRepository(db)
        deal = escrow_repo.create(
            deal_hash=_generate_deal_hash(buyer_id, seller_id, amount, now),
            buyer_id=buyer_id,
            seller_id=seller_id,
            amount=amount,
            description=description,
            accept_timeout_hours=accept_timeout_hours,
            delivery_timeout_hours=delivery_timeout_hours,
            review_timeout_hours=review_timeout_hours,
            fee_percent=self._fee_percent,
        )

        # Store deal metadata indicating multisig mode
        deal.deal_metadata = {"mode": "multisig"}
        db.flush()

        # Create the MultisigEscrow record
        timeout_at = now + timedelta(hours=_SETUP_TIMEOUT_HOURS)
        ms_repo = MultisigEscrowRepository(db)
        ms_escrow = ms_repo.create(
            escrow_deal_id=deal.id,
            state="setup_round_1",
            fee_collected=fee,
            funded_amount=funded_amount,
            timeout_at=timeout_at,
        )

        # Record fee collection
        db.add(FeeCollection(
            source_type="multisig_escrow",
            source_id=ms_escrow.id,
            amount=fee,
            token="XMR",
            status=FeeCollectionStatus.COLLECTED,
        ))
        db.flush()

        # Hub auto-submits round 1 data (prepare_multisig stub)
        hub_info = self._prepare_multisig_for_hub()
        ms_repo.add_round(
            multisig_escrow_id=ms_escrow.id,
            round_number=1,
            participant="hub",
            multisig_info=hub_info,
        )

        audit_log(
            action="multisig_escrow.created",
            agent_id=buyer_id,
            resource_type="multisig_escrow",
            resource_id=ms_escrow.id,
            details={
                "seller_id": str(seller_id),
                "amount": str(amount),
                "fee": str(fee),
                "funded_amount": str(funded_amount),
            },
            db=db,
        )

        return {
            "id": str(ms_escrow.id),
            "escrow_deal_id": str(deal.id),
            "fee_collected": str(fee),
            "funded_amount": str(funded_amount),
            "state": "setup_round_1",
            "timeout_at": _iso(timeout_at),
        }

    def submit_round(
        self,
        db: Session,
        escrow_id: UUID,
        participant: str,
        round_number: int,
        multisig_info: str,
    ) -> dict:
        """Submit multisig key exchange data for a round.

        If all 3 participants have submitted for the current round,
        the state automatically advances to the next round.
        """
        if participant not in _VALID_PARTICIPANTS:
            raise ValueError(
                f"participant must be one of {sorted(_VALID_PARTICIPANTS)}"
            )
        if round_number not in (1, 2, 3):
            raise ValueError("round_number must be 1, 2, or 3")

        ms_repo = MultisigEscrowRepository(db)
        ms_escrow = ms_repo.get_by_id_for_update(escrow_id)
        if ms_escrow is None:
            raise LookupError(f"Multisig escrow {escrow_id} not found")

        expected_state, next_state = _ROUND_STATE_MAP[round_number]
        if ms_escrow.state != expected_state:
            raise ValueError(
                f"Cannot submit round {round_number} data in state "
                f"'{ms_escrow.state}' (expected '{expected_state}')"
            )

        # Store the round submission
        ms_repo.add_round(
            multisig_escrow_id=escrow_id,
            round_number=round_number,
            participant=participant,
            multisig_info=multisig_info,
        )

        # Check if all participants have submitted for this round
        count = ms_repo.count_round_submissions(escrow_id, round_number)
        state_advanced = False

        if count >= _PARTICIPANTS_PER_ROUND:
            ms_repo.update_state(escrow_id, next_state)
            ms_escrow.state = next_state
            state_advanced = True

            # If we just completed round 3, the multisig address is ready
            if next_state == "funded":
                ms_escrow.multisig_address = self._finalize_multisig_address()
                db.flush()

        return {
            "escrow_id": str(escrow_id),
            "round_number": round_number,
            "participant": participant,
            "state": ms_escrow.state,
            "state_advanced": state_advanced,
            "submissions_count": count,
        }

    def get_state(self, db: Session, escrow_id: UUID) -> dict:
        """Get current multisig escrow state."""
        ms_repo = MultisigEscrowRepository(db)
        ms_escrow = ms_repo.get_by_id(escrow_id)
        if ms_escrow is None:
            raise LookupError(f"Multisig escrow {escrow_id} not found")
        return _multisig_to_dict(ms_escrow)

    def initiate_release(
        self,
        db: Session,
        escrow_id: UUID,
        initiator: str,
    ) -> dict:
        """Start release process (requires 2-of-3 signatures).

        The initiator creates a partially-signed transaction.
        Another participant must cosign to complete the release.
        """
        if initiator not in _VALID_PARTICIPANTS:
            raise ValueError(
                f"initiator must be one of {sorted(_VALID_PARTICIPANTS)}"
            )

        ms_repo = MultisigEscrowRepository(db)
        ms_escrow = ms_repo.get_by_id_for_update(escrow_id)
        if ms_escrow is None:
            raise LookupError(f"Multisig escrow {escrow_id} not found")

        if ms_escrow.state not in ("active", "funded"):
            raise ValueError(
                f"Cannot initiate release in state '{ms_escrow.state}'"
            )

        # Create partially signed release TX (stub)
        partial_tx = self._create_partial_release_tx(ms_escrow)

        ms_escrow.state = "releasing"
        ms_escrow.release_initiator = initiator
        ms_escrow.release_tx_hex = partial_tx
        db.flush()

        return {
            "escrow_id": str(escrow_id),
            "state": "releasing",
            "initiator": initiator,
            "partial_tx": partial_tx,
        }

    def cosign_release(
        self,
        db: Session,
        escrow_id: UUID,
        signer: str,
        signed_tx: str,
    ) -> dict:
        """Cosign the release transaction (2nd of 2 required signatures).

        Once cosigned, the funds are released and the escrow completes.
        """
        if signer not in _VALID_PARTICIPANTS:
            raise ValueError(
                f"signer must be one of {sorted(_VALID_PARTICIPANTS)}"
            )

        ms_repo = MultisigEscrowRepository(db)
        ms_escrow = ms_repo.get_by_id_for_update(escrow_id)
        if ms_escrow is None:
            raise LookupError(f"Multisig escrow {escrow_id} not found")

        if ms_escrow.state != "releasing":
            raise ValueError(
                f"Cannot cosign in state '{ms_escrow.state}' (expected 'releasing')"
            )

        if signer == ms_escrow.release_initiator:
            raise ValueError(
                "Cosigner must be different from the release initiator"
            )

        # Submit the cosigned TX (stub — would broadcast via wallet RPC)
        tx_hash = self._broadcast_signed_tx(signed_tx)

        ms_escrow.state = "completed"
        ms_escrow.funded_tx_hash = tx_hash
        db.flush()

        # Also complete the underlying EscrowDeal
        deal = ms_escrow.escrow_deal
        if deal and deal.status != EscrowStatus.COMPLETED:
            deal.status = EscrowStatus.COMPLETED
            deal.completed_at = _now()
            db.flush()

        return {
            "escrow_id": str(escrow_id),
            "state": "completed",
            "tx_hash": tx_hash,
        }

    def dispute(
        self,
        db: Session,
        escrow_id: UUID,
        disputer: str,
        reason: str,
    ) -> dict:
        """Raise a dispute.  Hub mediates resolution."""
        if disputer not in _VALID_PARTICIPANTS:
            raise ValueError(
                f"disputer must be one of {sorted(_VALID_PARTICIPANTS)}"
            )
        if not reason or not reason.strip():
            raise ValueError("Dispute reason must not be empty")

        ms_repo = MultisigEscrowRepository(db)
        ms_escrow = ms_repo.get_by_id_for_update(escrow_id)
        if ms_escrow is None:
            raise LookupError(f"Multisig escrow {escrow_id} not found")

        if ms_escrow.state in ("completed", "cancelled"):
            raise ValueError(
                f"Cannot dispute escrow in terminal state '{ms_escrow.state}'"
            )

        ms_escrow.state = "disputed"
        ms_escrow.disputed_by = disputer
        ms_escrow.dispute_reason = reason
        db.flush()

        audit_log(
            action="multisig_escrow.disputed",
            resource_type="multisig_escrow",
            resource_id=ms_escrow.id,
            details={
                "disputed_by": disputer,
                "reason": reason,
            },
            db=db,
        )

        return {
            "escrow_id": str(escrow_id),
            "state": "disputed",
            "disputed_by": disputer,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # Wallet RPC stubs (to be connected to actual Monero wallet RPC)
    # ------------------------------------------------------------------

    def _prepare_multisig_for_hub(self) -> str:
        """Stub: call prepare_multisig on the hub wallet.

        Returns the hub's multisig info string for round 1.
        In production, this calls wallet RPC prepare_multisig.
        """
        if self._wallet is not None:
            return self._wallet.prepare_multisig()
        return f"hub_multisig_info_{secrets.token_hex(16)}"

    def _finalize_multisig_address(self) -> str:
        """Stub: derive the final multisig address after round 3.

        In production, calls wallet RPC finalize_multisig and returns
        the shared multisig address.
        """
        if self._wallet is not None:
            return self._wallet.finalize_multisig()
        return f"multisig_address_{secrets.token_hex(16)}"

    def _create_partial_release_tx(self, ms_escrow: MultisigEscrow) -> str:
        """Stub: create a partially-signed release transaction.

        In production, calls wallet RPC transfer with partial signing.
        """
        if self._wallet is not None:
            return self._wallet.create_partial_tx(
                amount=ms_escrow.funded_amount,
                address=ms_escrow.multisig_address,
            )
        return f"partial_tx_{secrets.token_hex(32)}"

    def _broadcast_signed_tx(self, signed_tx: str) -> str:
        """Stub: broadcast a fully-signed transaction.

        In production, calls wallet RPC submit_multisig and returns tx hash.
        """
        if self._wallet is not None:
            return self._wallet.submit_multisig(signed_tx)
        return f"tx_hash_{secrets.token_hex(32)}"
