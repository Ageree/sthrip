"""
SwapService — business logic for cross-chain HTLC swaps.

Flow:
  1. create_swap() — generate HTLC secret/hash, store order in CREATED state.
  2. (external) BTC tx observed → lock() the order.
  3. claim_swap() — verify HTLC secret, COMPLETED, credit XMR balance.

The HTLC secret is stored in the order record at creation so the swap
initiator can retrieve it for the claim step.  The secret is NOT returned
in the create response (see security note in docs).
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.swap_repo import SwapRepository
from sthrip.db.balance_repo import BalanceRepository
from sthrip.db.models import SwapOrder, SwapStatus
from sthrip.services.rate_service import RateService

logger = logging.getLogger("sthrip.swap_service")

_LOCK_EXPIRY_MINUTES: int = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _order_to_dict(order: SwapOrder) -> dict:
    """Convert a SwapOrder ORM object to a serialisable dict.

    The htlc_secret is intentionally omitted for security; it is only
    stored in the DB for the claim step and must not leak via API responses.
    """
    state_val = order.state.value if hasattr(order.state, "value") else order.state
    return {
        "swap_id": str(order.id),
        "from_agent_id": str(order.from_agent_id),
        "from_currency": order.from_currency,
        "from_amount": str(order.from_amount),
        "to_currency": order.to_currency,
        "to_amount": str(order.to_amount),
        "exchange_rate": str(order.exchange_rate),
        "fee_amount": str(order.fee_amount),
        "state": state_val,
        "htlc_hash": order.htlc_hash,
        "btc_tx_hash": order.btc_tx_hash,
        "xmr_tx_hash": order.xmr_tx_hash,
        "lock_expiry": _iso(order.lock_expiry),
        "created_at": _iso(order.created_at),
    }


class SwapService:
    """Orchestrates cross-chain HTLC swap operations."""

    def __init__(self) -> None:
        self._rate_svc = RateService()

    # ------------------------------------------------------------------
    # Rate helpers
    # ------------------------------------------------------------------

    def get_rates(self) -> dict:
        """Return all supported pair rates."""
        return self._rate_svc.get_rates()

    def get_quote(
        self,
        from_currency: str,
        from_amount: Decimal,
        to_currency: str = "XMR",
    ) -> dict:
        """Return a swap quote for the given pair and amount."""
        return self._rate_svc.get_quote(from_currency, from_amount, to_currency)

    # ------------------------------------------------------------------
    # Swap lifecycle
    # ------------------------------------------------------------------

    def create_swap(
        self,
        db: Session,
        from_agent_id: UUID,
        from_currency: str,
        from_amount: Decimal,
        to_currency: str = "XMR",
    ) -> dict:
        """Create a new swap order.

        Steps:
          1. Get quote from RateService.
          2. Generate HTLC: secret = token_hex(32), hash = SHA-256(secret bytes).
          3. Persist order with lock_expiry = now + 30 minutes.
          4. Return order dict (htlc_secret NOT in response for security).

        The htlc_secret is stored in the order record so the initiator can
        retrieve it via get_swap() or from the creation response in a real
        HTLC protocol.
        """
        quote = self._rate_svc.get_quote(from_currency, from_amount, to_currency)

        htlc_secret = secrets.token_hex(32)
        htlc_hash = hashlib.sha256(bytes.fromhex(htlc_secret)).hexdigest()
        lock_expiry = _now() + timedelta(minutes=_LOCK_EXPIRY_MINUTES)

        repo = SwapRepository(db)
        order = repo.create(
            from_agent_id=from_agent_id,
            from_currency=from_currency,
            from_amount=from_amount,
            to_currency=quote["to_currency"],
            to_amount=Decimal(quote["to_amount"]),
            exchange_rate=Decimal(quote["rate"]),
            fee_amount=Decimal(quote["fee"]),
            htlc_hash=htlc_hash,
            lock_expiry=lock_expiry,
        )

        # Store the pre-image so the initiator can claim later.
        # In production this would be returned via a secure separate channel.
        order.htlc_secret = htlc_secret
        db.flush()

        result = _order_to_dict(order)
        # Explicitly exclude htlc_secret from the returned dict.
        result.pop("htlc_secret", None)
        return result

    def claim_swap(
        self,
        db: Session,
        order_id: UUID,
        agent_id: UUID,
        htlc_secret: str,
    ) -> dict:
        """Claim a locked swap by revealing the HTLC pre-image.

        Steps:
          1. Retrieve order; raise LookupError if not found.
          2. Verify agent ownership; raise PermissionError if mismatch.
          3. Verify SHA-256(htlc_secret bytes) == order.htlc_hash;
             raise ValueError on mismatch.
          4. Transition LOCKED → COMPLETED.
          5. Credit to_amount to agent's XMR balance.
          6. Return updated order dict.

        Raises:
            LookupError: order not found.
            PermissionError: caller is not the swap initiator.
            ValueError: wrong HTLC secret or order not in LOCKED state.
        """
        repo = SwapRepository(db)
        order = repo.get_by_id(order_id)
        if order is None:
            raise LookupError(f"Swap order {order_id} not found")

        if order.from_agent_id != agent_id:
            raise PermissionError("You do not own this swap order")

        # Verify HTLC pre-image
        computed_hash = hashlib.sha256(bytes.fromhex(htlc_secret)).hexdigest()
        if computed_hash != order.htlc_hash:
            raise ValueError(f"Invalid HTLC secret for order {order_id}")

        rows = repo.complete(order_id, htlc_secret=htlc_secret)
        if rows == 0:
            state_val = order.state.value if hasattr(order.state, "value") else order.state
            raise ValueError(
                f"Cannot claim swap in state '{state_val}'. Order must be LOCKED."
            )

        # Credit XMR balance to the agent
        balance_repo = BalanceRepository(db)
        balance_repo.credit(agent_id, Decimal(str(order.to_amount)), token="XMR")

        db.flush()
        db.refresh(order)
        return _order_to_dict(order)

    def get_swap(
        self,
        db: Session,
        order_id: UUID,
        agent_id: UUID,
    ) -> dict:
        """Return swap order dict for the owning agent.

        Raises:
            LookupError: order not found.
            PermissionError: caller does not own the order.
        """
        repo = SwapRepository(db)
        order = repo.get_by_id(order_id)
        if order is None:
            raise LookupError(f"Swap order {order_id} not found")
        if order.from_agent_id != agent_id:
            raise PermissionError("You do not own this swap order")
        return _order_to_dict(order)

    def expire_stale(self, db: Session) -> int:
        """Expire all overdue swap orders (past lock_expiry and in CREATED/LOCKED).

        Returns the number of orders expired.
        """
        repo = SwapRepository(db)
        stale = repo.get_expired()
        count = 0
        for order in stale:
            rows = repo.expire(order.id)
            count += rows
        return count
