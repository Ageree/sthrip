"""Payment dispatch — local vs TEE proxy with feature-flag + fall-back.

Phase 3 Sprint 6. Sits between the FastAPI router (``api/routers/payments.py``)
and the actual payment handler.  Reads ``STHRIP_PAYMENT_VIA_TEE`` and either:

* runs the existing local ``_execute_hub_transfer`` logic (default), or
* proxies to the TEE service on a GCP Confidential VM and writes the
  HubRoute admin/dashboard row locally **after** the TEE returns success
  (the Sprint 5 carry-over M-1 fix — TEE keeps payment-correctness state
  only; HubRoute is Railway-side metadata).

The dispatcher returns the SAME route-dict shape that
``_execute_hub_transfer`` produces today, so the router code path is
unchanged regardless of mode.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Optional
from uuid import UUID

from sthrip.services import payment_tee_client
from sthrip.services.metrics import tee_dispatch_total, tee_unreachable_total
from sthrip.services.payment_tee_client import (
    TEEServerError,
    TEEUnreachableError,
)

logger = logging.getLogger("sthrip.payment_dispatch")


_ENV_FLAG = "STHRIP_PAYMENT_VIA_TEE"


def is_tee_enabled() -> bool:
    """Read ``STHRIP_PAYMENT_VIA_TEE`` — default ``false``.

    Accepts the conventional truthy strings (``1``, ``true``, ``TRUE``,
    ``yes``).  Anything else, or unset, is False.
    """
    raw = os.getenv(_ENV_FLAG, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Local mode — wraps the existing _execute_hub_transfer
# ---------------------------------------------------------------------------


def _local_dispatch(
    db,
    agent,
    recipient,
    amount: Decimal,
    fee_info: Dict[str, Any],
    req,
    idempotency_key: Optional[str],
    fee_collector=None,
) -> Dict[str, Any]:
    """Call the existing local handler with the same args the router uses.

    Imported lazily because ``api.routers.payments`` imports this module's
    parent package indirectly (Pydantic schema imports), so a top-level
    import would create a circular import.
    """
    from api.routers.payments import _execute_hub_transfer

    return _execute_hub_transfer(
        db,
        agent,
        recipient,
        amount,
        fee_info,
        req,
        idempotency_key,
        fee_collector=fee_collector,
    )


# ---------------------------------------------------------------------------
# TEE mode — call TEE, write HubRoute row locally on success
# ---------------------------------------------------------------------------


def _build_envelope(agent, recipient, amount: Decimal, req) -> Dict[str, Any]:
    """Canonical request envelope handed to the TEE service.

    Field names match ``payment_service.HubPaymentEnvelope`` (Sprint 5).
    """
    return {
        "from_agent_id": str(agent.id),
        "to_agent_id": str(recipient.id),
        "to_agent_name": recipient.agent_name,
        "to_xmr_address": recipient.xmr_address,
        "amount": amount,
        "urgency": getattr(req, "urgency", "normal"),
        "memo": getattr(req, "memo", None),
    }


def _write_hub_route_after_tee(
    db,
    *,
    payment_id: str,
    from_agent_id: UUID,
    to_agent_id: UUID,
    amount: Decimal,
    fee_amount: Decimal,
    fee_percent: Decimal,
    urgency: str,
) -> None:
    """Insert the HubRoute admin/dashboard row Railway-side.

    The TEE service deliberately skips this write (see Sprint 5
    ``payment_service.py`` docstring — keeps the TEE narrow). Sprint 6
    fixes the resulting drift by inserting the row here after the TEE
    confirms success.

    Idempotent: if a row with this ``payment_id`` already exists (a retry
    after the TEE responded but Railway crashed mid-write), we update it
    in-place to CONFIRMED instead of inserting a duplicate.
    """
    from sthrip.db.models import HubRoute, HubRouteStatus

    existing = (
        db.query(HubRoute).filter(HubRoute.payment_id == payment_id).first()
    )
    now = datetime.now(timezone.utc)
    if existing is not None:
        # Move PENDING (if it was created earlier) to CONFIRMED. If
        # already CONFIRMED, leave it alone.
        if existing.status == HubRouteStatus.PENDING:
            existing.status = HubRouteStatus.CONFIRMED
            existing.confirmed_at = now
            existing.fee_amount = fee_amount
            existing.fee_collected = True
            existing.fee_collected_at = now
        return

    route = HubRoute(
        payment_id=payment_id,
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        amount=amount,
        token="XMR",
        fee_percent=fee_percent,
        fee_amount=fee_amount,
        fee_collected=True,
        fee_collected_at=now,
        instant_confirmation=(urgency == "urgent"),
        status=HubRouteStatus.CONFIRMED,
        confirmed_at=now,
    )
    db.add(route)
    db.flush()


def _tee_response_to_route_dict(
    tee_response: Dict[str, Any],
    *,
    agent,
    recipient,
    amount: Decimal,
    fee_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Translate the TEE's ``HubPaymentResponse`` to the local route dict.

    Local handler returns a dict with keys ``payment_id``, ``status``,
    ``amount``, ``fee`` (a dict — the fee breakdown), ``from_agent_id``,
    ``to_agent_id`` etc. The TEE returns a flat dict with ``payment_id``,
    ``status``, ``amount``, ``fee`` (string), ``fee_percent``.

    We mutate ``fee_info`` in-place so the router's response builder sees
    the *real* commission fee, exactly like the local path does.
    """
    fee_amount_xmr = Decimal(tee_response.get("fee", "0"))
    fee_percent = Decimal(tee_response.get("fee_percent", "0"))

    fee_info["fee_amount"] = fee_amount_xmr
    fee_info["fee_percent"] = fee_percent
    fee_info["total_deduction"] = Decimal(amount) + fee_amount_xmr

    return {
        "payment_id": tee_response["payment_id"],
        "from_agent_id": str(agent.id),
        "to_agent_id": str(recipient.id),
        "amount": amount,
        "token": "XMR",
        "fee": {
            "fee_amount": fee_amount_xmr,
            "fee_percent": fee_percent,
            "tier_discount": agent.tier.value,
            "seller_receives": Decimal(amount) - fee_amount_xmr,
        },
        "status": tee_response.get("status", "confirmed"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        # No "duplicate" key — TEE collapses duplicates server-side and
        # returns 2xx with the original payment_id; the router does its
        # own idempotency check upstream.
    }


def _tee_dispatch(
    db,
    agent,
    recipient,
    amount: Decimal,
    fee_info: Dict[str, Any],
    req,
    idempotency_key: Optional[str],
    fee_collector=None,
    *,
    fallback: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Proxy a hub-routing payment to the TEE.

    On TEE network/5xx error → fall back to ``fallback`` (default: local
    handler).

    On TEE 4xx → return the body unchanged (no fall-back; legitimate
    user-error like insufficient balance).

    On TEE 2xx → write the HubRoute row locally (M-1 fix) and return a
    route dict shaped like the local handler's output.
    """
    # Resolve the fallback at call-time so test patches of
    # ``payment_dispatch._local_dispatch`` are honoured.  Using a default
    # parameter, or even a closure over the name, would bind the original
    # reference too early; we deliberately reach into the module globals
    # so ``patch.object(payment_dispatch, "_local_dispatch", ...)`` works.
    if fallback is None:
        import sys as _sys

        def fallback(*a, **kw):  # type: ignore[no-redef]
            mod = _sys.modules[__name__]
            return mod._local_dispatch(*a, **kw)

    if not idempotency_key:
        # The Sprint 5 TEE service requires an idempotency_key (>=8 chars).
        # If the router didn't provide one, generate a deterministic one
        # from the request hash so retries are still safe.
        import hashlib

        seed = f"{agent.id}:{recipient.id}:{amount}:{getattr(req, 'urgency', 'normal')}"
        idempotency_key = "auto_" + hashlib.sha256(seed.encode()).hexdigest()[:24]

    envelope = _build_envelope(agent, recipient, amount, req)

    try:
        tee_response = payment_tee_client.dispatch_hub_routing(
            envelope, idempotency_key
        )
    except TEEUnreachableError as exc:
        logger.warning(
            "TEE unreachable, falling back to local handler: %s", exc
        )
        tee_unreachable_total.labels(reason="network").inc()
        tee_dispatch_total.labels(outcome="fallback_unreachable").inc()
        return fallback(
            db, agent, recipient, amount, fee_info, req, idempotency_key,
            fee_collector=fee_collector,
        )
    except TEEServerError as exc:
        logger.error(
            "TEE 5xx, falling back to local handler: %s", exc
        )
        tee_unreachable_total.labels(reason="server_error").inc()
        tee_dispatch_total.labels(outcome="fallback_5xx").inc()
        return fallback(
            db, agent, recipient, amount, fee_info, req, idempotency_key,
            fee_collector=fee_collector,
        )

    # 4xx — legitimate user-error from the TEE (e.g. insufficient balance).
    # Bubble it up exactly like the local handler would: as an HTTPException
    # with the same status. NO fall-back — falling back would re-charge.
    status_code = tee_response.get("_status_code")
    if status_code and 400 <= status_code < 500:
        from fastapi import HTTPException

        tee_dispatch_total.labels(outcome="user_error").inc()
        detail = tee_response.get("detail", "TEE rejected the request")
        raise HTTPException(status_code=status_code, detail=detail)

    # 2xx — TEE wrote balance + transaction + fee_collection rows.
    # Now write the HubRoute admin row locally (M-1 fix).
    _write_hub_route_after_tee(
        db,
        payment_id=tee_response["payment_id"],
        from_agent_id=agent.id if isinstance(agent.id, UUID) else UUID(str(agent.id)),
        to_agent_id=UUID(str(recipient.id)) if not isinstance(recipient.id, UUID) else recipient.id,
        amount=amount,
        fee_amount=Decimal(tee_response.get("fee", "0")),
        fee_percent=Decimal(tee_response.get("fee_percent", "0")),
        urgency=getattr(req, "urgency", "normal"),
    )

    tee_dispatch_total.labels(outcome="success").inc()
    return _tee_response_to_route_dict(
        tee_response,
        agent=agent,
        recipient=recipient,
        amount=amount,
        fee_info=fee_info,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def dispatch_hub_routing(
    db,
    agent,
    recipient,
    amount: Decimal,
    fee_info: Dict[str, Any],
    req,
    idempotency_key: Optional[str],
    fee_collector=None,
) -> Dict[str, Any]:
    """Route a hub-payment request to either the local handler or the TEE.

    Returns
    -------
    dict
        Same shape as ``api.routers.payments._execute_hub_transfer``.
    """
    if is_tee_enabled():
        logger.debug("dispatching hub_routing via TEE proxy")
        return _tee_dispatch(
            db, agent, recipient, amount, fee_info, req, idempotency_key,
            fee_collector=fee_collector,
        )

    logger.debug("dispatching hub_routing via local handler")
    return _local_dispatch(
        db, agent, recipient, amount, fee_info, req, idempotency_key,
        fee_collector=fee_collector,
    )


# ---------------------------------------------------------------------------
# Health-check loop — runs in the FastAPI lifespan
# ---------------------------------------------------------------------------


_HEALTH_INTERVAL_S = 60.0
_HEALTH_FAIL_ALERT_THRESHOLD = 3


async def health_check_loop(
    *,
    interval_s: float = _HEALTH_INTERVAL_S,
    threshold: int = _HEALTH_FAIL_ALERT_THRESHOLD,
    iterations: Optional[int] = None,
) -> None:
    """Background coroutine — pings the TEE ``/health`` every ``interval_s``.

    * If the flag is OFF when the loop wakes up, it logs once and returns.
    * On 3 consecutive failures it logs an alert and increments
      ``tee_unreachable_total{reason="health"}``.  It does NOT auto-flip
      the feature flag — operator owns that decision.
    * ``iterations`` lets tests bound the run.
    """
    import asyncio

    if not is_tee_enabled():
        logger.info(
            "STHRIP_PAYMENT_VIA_TEE is off; skipping TEE health-check loop"
        )
        return

    consecutive_failures = 0
    count = 0
    while True:
        ok = False
        try:
            ok = payment_tee_client.health_check()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("TEE health check raised: %s", exc)
            ok = False

        if ok:
            if consecutive_failures:
                logger.info(
                    "TEE health recovered after %d failures",
                    consecutive_failures,
                )
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            tee_unreachable_total.labels(reason="health").inc()
            if consecutive_failures >= threshold:
                logger.warning(
                    "TEE /health failed %d consecutive times — operator "
                    "should review (feature flag is NOT auto-flipped)",
                    consecutive_failures,
                )

        count += 1
        if iterations is not None and count >= iterations:
            return

        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return
