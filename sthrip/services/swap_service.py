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
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.swap_repo import SwapRepository
from sthrip.db.balance_repo import BalanceRepository
from sthrip.db.models import SwapOrder, SwapStatus
from sthrip.services.rate_service import RateService
from sthrip.services.exchange_providers import (
    create_order_with_fallback,
    ExchangeProviderError,
    ChangeNowProvider,
    SideShiftProvider,
    STATUS_FINISHED,
    STATUS_FAILED,
    STATUS_EXPIRED,
)

logger = logging.getLogger("sthrip.swap_service")

_LOCK_EXPIRY_MINUTES: int = 30

# Allowed format for external order IDs returned by exchange providers.
_EXTERNAL_ORDER_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")

# XMR address where the exchange should send converted funds.
# In production this is the hub's primary XMR wallet address.
_XMR_HUB_ADDRESS_ENV = "XMR_HUB_ADDRESS"


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
        # Exchange provider fields (None for legacy HTLC-only orders)
        "external_order_id": getattr(order, "external_order_id", None),
        "deposit_address": getattr(order, "deposit_address", None),
        "provider_name": getattr(order, "provider_name", None),
    }


class SwapService:
    """Orchestrates cross-chain HTLC swap operations.

    Uses ChangeNOW (primary) / SideShift (fallback) to create real deposit
    addresses for incoming funds. XMR is credited to the agent's hub balance
    once the exchange confirms delivery.
    """

    def __init__(self) -> None:
        self._rate_svc = RateService()

    def _get_hub_xmr_address(self) -> str:
        """Return the hub's XMR receive address for exchange deliveries.

        Reads XMR_HUB_ADDRESS from the environment.  Raises RuntimeError if
        not configured (should be set in Railway env vars for production).
        """
        import os
        addr = os.environ.get(_XMR_HUB_ADDRESS_ENV, "")
        if not addr:
            raise RuntimeError(
                f"XMR_HUB_ADDRESS is not configured. "
                "Set this env var to the hub's primary XMR wallet address."
            )
        return addr

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
        """Create a new swap order via a real exchange provider (ChangeNOW / SideShift).

        Steps:
          1. Get quote from RateService.
          2. Generate HTLC hash (kept for compatibility; secret stored for legacy claim path).
          3. Persist order in CREATED state with lock_expiry = now + 30 minutes.
          4. Call exchange providers (ChangeNOW first, SideShift fallback) to get
             a real deposit address.
          5. Store external_order_id, deposit_address, provider_name on the order.
          6. Return order dict — deposit_address tells user where to send funds.

        Falls back gracefully if exchange providers are unavailable (returns the
        order without a deposit_address so legacy HTLC flow still works).
        """
        quote = self._rate_svc.get_quote(from_currency, from_amount, to_currency)

        htlc_secret = secrets.token_hex(32)
        htlc_hash = hashlib.sha256(bytes.fromhex(htlc_secret)).hexdigest()
        lock_expiry = _now() + timedelta(minutes=_LOCK_EXPIRY_MINUTES)

        repo = SwapRepository(db)

        # Attempt to obtain a real deposit address from an exchange provider.
        # We do this before persisting so we know whether to store the HTLC
        # secret (only needed for the legacy claim path, not exchange swaps).
        provider_result = None
        try:
            hub_xmr_address = self._get_hub_xmr_address()
            provider_result = create_order_with_fallback(
                from_currency=from_currency,
                from_amount=str(from_amount),
                to_currency=quote["to_currency"],
                to_address=hub_xmr_address,
            )
        except (ExchangeProviderError, RuntimeError) as exc:
            # Non-fatal: log and continue.  The order exists; the poller or
            # legacy HTLC path will handle it.
            logger.warning(
                "exchange provider unavailable for swap: %s — proceeding without deposit_address",
                exc,
            )

        # For exchange-provider swaps the HTLC secret is not used; only
        # store it when falling back to the legacy claim path.
        stored_secret = None if provider_result is not None else htlc_secret

        order = repo.create(
            from_agent_id=from_agent_id,
            from_currency=from_currency,
            from_amount=from_amount,
            to_currency=quote["to_currency"],
            to_amount=Decimal(quote["to_amount"]),
            exchange_rate=Decimal(quote["rate"]),
            fee_amount=Decimal(quote["fee"]),
            htlc_hash=htlc_hash,
            htlc_secret=stored_secret,
            lock_expiry=lock_expiry,
        )

        if provider_result is not None:
            ext_id = provider_result["external_order_id"]
            if not _EXTERNAL_ORDER_ID_RE.match(ext_id):
                raise ValueError(
                    f"Invalid external_order_id format from provider: {ext_id!r}"
                )
            rows = repo.set_external_order(
                swap_id=order.id,
                external_order_id=ext_id,
                deposit_address=provider_result["deposit_address"],
                provider_name=provider_result["provider"],
            )
            if rows == 0:
                logger.warning("set_external_order matched 0 rows for swap %s", order.id)
            db.flush()
            db.refresh(order)
            logger.info(
                "swap %s created via %s — deposit to %s",
                order.id,
                provider_result["provider"],
                provider_result["deposit_address"],
            )

        result = _order_to_dict(order)
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

        # Validate hex format before attempting conversion
        try:
            secret_bytes = bytes.fromhex(htlc_secret)
        except ValueError:
            raise ValueError("Invalid HTLC secret: must be 64 hex characters")

        # Verify HTLC pre-image (constant-time comparison to prevent timing attacks)
        computed_hash = hashlib.sha256(secret_bytes).hexdigest()
        if not hmac.compare_digest(computed_hash, order.htlc_hash):
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

    def get_pending_external_orders(self, db: Session) -> List[SwapOrder]:
        """Return all CREATED orders that have an external_order_id.

        Used by the background poller to check exchange status.
        """
        repo = SwapRepository(db)
        return repo.get_pending_external()

    def poll_external_orders(self, db: Session) -> dict:
        """Check exchange status for all pending external orders.

        For each CREATED order with an external_order_id:
          - Determines which provider to use (from provider_name field).
          - Calls get_order_status() on the provider.
          - If FINISHED: transitions CREATED → COMPLETED, credits XMR balance.
          - If FAILED or EXPIRED: transitions CREATED → EXPIRED.
          - If any other status: leaves the order alone (still waiting).

        Returns a summary dict: {completed: int, expired: int, errors: int, skipped: int}
        """
        repo = SwapRepository(db)
        balance_repo = BalanceRepository(db)
        orders = repo.get_pending_external()

        completed = 0
        expired_count = 0
        errors = 0
        skipped = 0

        _provider_cache: dict = {}

        for order in orders:
            provider_name = getattr(order, "provider_name", None)
            external_order_id = order.external_order_id

            # external_order_id is guaranteed non-null by the query filter;
            # this guard is defensive only.
            if not external_order_id or not provider_name:
                skipped += 1
                continue

            # Reuse provider instances across orders in the same poll cycle.
            # Explicit matching — never fall back silently to a default provider.
            if provider_name not in _provider_cache:
                if provider_name == "changenow":
                    _provider_cache[provider_name] = ChangeNowProvider()
                elif provider_name == "sideshift":
                    _provider_cache[provider_name] = SideShiftProvider()
                else:
                    logger.error(
                        "Unknown provider %r for swap %s — skipping",
                        provider_name,
                        order.id,
                    )
                    errors += 1
                    continue

            provider = _provider_cache[provider_name]

            try:
                status_info = provider.get_order_status(external_order_id)
                status = status_info["status"]
                to_amount_raw = status_info.get("to_amount")

                if status == STATUS_FINISHED:
                    to_amount = (
                        Decimal(to_amount_raw)
                        if to_amount_raw
                        else order.to_amount
                    )
                    rows = repo.complete_from_external(
                        swap_id=order.id,
                        to_amount=to_amount,
                    )
                    if rows == 1:
                        balance_repo.credit(
                            order.from_agent_id,
                            Decimal(str(to_amount)),
                            token="XMR",
                        )
                        # Per-order commit to prevent crash-recovery double-credit
                        db.commit()
                        completed += 1
                        logger.info(
                            "swap %s completed via %s — credited %.8f XMR",
                            order.id,
                            provider_name,
                            to_amount,
                        )
                    else:
                        logger.warning(
                            "complete_from_external matched 0 rows for swap %s (already completed?)",
                            order.id,
                        )
                        skipped += 1

                elif status in (STATUS_FAILED, STATUS_EXPIRED):
                    rows = repo.expire(order.id)
                    if rows == 1:
                        db.commit()
                        expired_count += 1
                        logger.info(
                            "swap %s marked EXPIRED (exchange status: %s)",
                            order.id,
                            status,
                        )
                    else:
                        skipped += 1
                else:
                    # Still waiting/confirming — nothing to do yet.
                    skipped += 1

            except ExchangeProviderError as exc:
                logger.warning(
                    "poll_external_orders: provider error for swap %s: %s",
                    order.id,
                    exc,
                )
                errors += 1
            except Exception:
                logger.exception(
                    "poll_external_orders: unexpected error for swap %s",
                    order.id,
                )
                errors += 1

        return {
            "completed": completed,
            "expired": expired_count,
            "errors": errors,
            "skipped": skipped,
        }

    def expire_stale(self, db: Session) -> int:
        """Expire all overdue swap orders (past lock_expiry and in CREATED/LOCKED).

        Uses a single bulk UPDATE for efficiency instead of N+1 queries.
        Returns the number of orders expired.
        """
        repo = SwapRepository(db)
        return repo.bulk_expire_stale()
