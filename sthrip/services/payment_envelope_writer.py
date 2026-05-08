"""Repo-level dual-write helper for the payment-graph envelope.

Sprint 3 keeps every existing ``*_repo.create_*`` signature unchanged. Repos
call ``apply_envelope(model, payload)`` after constructing the ORM object
and before the session ``flush()``; the helper:

1. Skips silently if envelope columns are absent on the model (defensive —
   should never happen post-migration, but keeps unit tests from exploding
   if a fixture only includes a subset of tables).
2. Skips if the model already has a non-null ``participant_envelope``
   (idempotency — re-using a model instance for a write retry must not
   re-encrypt with a fresh DEK and stomp the previous envelope).
3. Encrypts the participants + amount + description and writes both
   ``participant_envelope`` and (where present) ``amount_bucket``.

Failure to encrypt raises — Sprint 3's goal is "no new graph rows without an
envelope". A misconfigured KEK should crash the request, not silently leak
plaintext-only rows for an attacker to harvest later.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Mapping, Optional

from .envelope_crypto import (
    PaymentEnvelope,
    amount_to_bucket,
    encrypt_envelope,
    load_hub_kek,
)
from .operator_keystore import get_keystore

logger = logging.getLogger(__name__)


def _serializable(value: Any) -> Any:
    """Coerce common ORM types (UUID, Decimal) to JSON-friendly primitives."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return value


def build_envelope(
    *,
    from_agent_id: Any,
    to_agent_id: Any,
    amount: Optional[Decimal],
    description: Optional[str],
) -> PaymentEnvelope:
    """Encrypt a payment-graph payload using the configured keys.

    Centralizes KEK loading so repo callers never see the raw key material.
    Raises ``RuntimeError`` if ``STHRIP_HUB_KEK`` is unset.
    """
    payload: Mapping[str, Any] = {
        "from_agent_id": _serializable(from_agent_id),
        "to_agent_id": _serializable(to_agent_id),
        "amount": _serializable(amount),
        "description": description,
    }
    hub_kek = load_hub_kek()
    op_kek = get_keystore().get_kek_for_envelope()
    return encrypt_envelope(dict(payload), hub_kek, op_kek)


def apply_envelope(
    model: Any,
    *,
    from_agent_id: Any,
    to_agent_id: Any,
    amount: Optional[Decimal] = None,
    description: Optional[str] = None,
) -> None:
    """Populate envelope columns on an ORM model in-place.

    Mutates ``model.participant_envelope`` and ``model.amount_bucket`` (if
    present). The function is intentionally a setter — we cannot return a new
    model copy because SQLAlchemy ORM objects are reference-tracked by the
    session. The "immutable input → new output" rule applies to
    :class:`PaymentEnvelope` itself (frozen dataclass), which is what we
    serialize into ``participant_envelope``.

    Idempotent: a non-null existing envelope is preserved.
    """
    if not hasattr(model, "participant_envelope"):
        # Old test schema or unrelated model — skip.
        return
    if getattr(model, "participant_envelope", None) is not None:
        # Already populated; respect prior write to keep DEKs stable.
        return

    envelope = build_envelope(
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        amount=amount,
        description=description,
    )
    model.participant_envelope = envelope.to_bytes()

    if hasattr(model, "amount_bucket") and amount is not None:
        try:
            bucket = amount_to_bucket(Decimal(str(amount)))
        except Exception:  # pragma: no cover - defensive
            bucket = None
        if bucket:
            model.amount_bucket = bucket
