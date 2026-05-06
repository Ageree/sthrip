"""Read-side counterpart to ``payment_envelope_writer`` (Sprint 4a).

This module gives repos a feature-flag-gated way to surface envelope-encrypted
participant data instead of the plaintext FK columns. It is non-destructive:
every fallback path returns the legacy FK value, so flipping the flag off is
always safe.

Behaviour
---------

When ``STHRIP_READ_FROM_ENVELOPE`` is unset or falsy (default), all callers
should treat the row as plaintext-only. The flag is checked via
``feature_flag_enabled()``.

When the flag is true and the row carries a non-null
``participant_envelope``, the reader attempts to decrypt with the configured
hub KEK plus the operator keystore. On any failure (envelope blob corrupt,
KEK mismatch, keystore unavailable) the reader emits a single warning and
returns ``ReadResult.source = "fallback_decrypt_error"`` with the row's
plaintext FKs. We never raise from a read path — Sprint 4a is dual-read,
not envelope-only.

The reader is intentionally pure: it does not mutate the ORM row; callers
either swap the row attributes (repo internals) or render through the
result dataclass (admin views).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional
from uuid import UUID

from .envelope_crypto import PaymentEnvelope, decrypt_envelope, load_hub_kek
from .operator_keystore import get_keystore

logger = logging.getLogger(__name__)


#: Env var that toggles envelope-first reads. Default False keeps Sprint 3
#: behaviour exactly. Operators flip in staging first, then prod.
_FLAG_ENV_VAR = "STHRIP_READ_FROM_ENVELOPE"

#: Strings interpreted as "true". Any other value is treated as false.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


ReadSource = Literal[
    "envelope",
    "fallback_fk",
    "fallback_envelope_null",
    "fallback_decrypt_error",
    "flag_off",
]


@dataclass(frozen=True)
class ReadResult:
    """Immutable view of what the reader resolved for a single row.

    ``source`` lets callers (and tests) tell which branch fired without
    threading bool flags through every API.
    """

    from_agent_id: Optional[Any]
    to_agent_id: Optional[Any]
    amount: Optional[Decimal]
    description: Optional[str]
    source: ReadSource


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


def feature_flag_enabled() -> bool:
    """Return True when reads should consult the encrypted envelope first.

    Reads the env var fresh every call — there is no cache. Tests toggle
    the flag through ``monkeypatch.setenv``; production flips it via Railway
    environment.
    """
    raw = os.environ.get(_FLAG_ENV_VAR, "")
    return raw.strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Pure decrypt
# ---------------------------------------------------------------------------


def read_payload_or_none(row: Any) -> Optional[dict]:
    """Decrypt ``row.participant_envelope`` and return the payload dict.

    Returns ``None`` if the row has no envelope, the envelope is
    unparseable, or decryption fails. Never raises — this function is
    intended for read paths that must degrade gracefully.
    """
    blob = getattr(row, "participant_envelope", None)
    if not blob:
        return None
    try:
        envelope = PaymentEnvelope.from_bytes(bytes(blob))
        hub_kek = load_hub_kek()
        op_kek = get_keystore().get_kek_for_envelope()
        return decrypt_envelope(envelope, hub_kek, op_kek)
    except Exception as exc:  # noqa: BLE001 — read paths must not raise
        # Log without including row IDs (those are the very thing we're
        # trying to encrypt). Operators investigate via row counts only.
        logger.warning(
            "payment_envelope_reader: decrypt failed (%s); falling back to FK",
            type(exc).__name__,
        )
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_uuid(value: Any) -> Optional[Any]:
    """Convert an envelope-decoded id back to UUID when possible.

    Envelope payloads serialise UUIDs as strings (see
    ``envelope_crypto._normalize_payload``). Callers that compare against
    ORM UUID values need a UUID instance back. If parsing fails we return
    the raw string — repo code already handles both.
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except (ValueError, AttributeError):
            return value
    return value


def _coerce_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Dual-read with fallback
# ---------------------------------------------------------------------------


def _row_fallback(row: Any, source: ReadSource) -> ReadResult:
    """Build a ReadResult from the row's plaintext FK columns."""
    # Different models name the columns differently. Probe in priority order.
    from_id = (
        getattr(row, "from_agent_id", None)
        or getattr(row, "buyer_id", None)
    )
    to_id = (
        getattr(row, "to_agent_id", None)
        or getattr(row, "seller_id", None)
    )
    amount = _coerce_decimal(getattr(row, "amount", None))
    description = (
        getattr(row, "description", None)
        if hasattr(row, "description")
        else getattr(row, "memo", None)
    )
    return ReadResult(
        from_agent_id=from_id,
        to_agent_id=to_id,
        amount=amount,
        description=description,
        source=source,
    )


def read_with_fallback(row: Any) -> ReadResult:
    """Resolve participant + amount + description for a payment-graph row.

    Decision tree:

    1. If the feature flag is off → return FK fallback with ``source="flag_off"``.
    2. If envelope is null/missing → ``source="fallback_envelope_null"``.
    3. If envelope decrypts cleanly → ``source="envelope"``.
    4. If envelope decrypt fails → ``source="fallback_decrypt_error"``.

    The fallback ALWAYS uses the row's plaintext FKs, which Sprint 3
    guarantees are populated. Sprint 4b drops the FKs; until then this
    keeps reads bullet-proof.
    """
    if not feature_flag_enabled():
        return _row_fallback(row, source="flag_off")

    blob = getattr(row, "participant_envelope", None)
    if not blob:
        return _row_fallback(row, source="fallback_envelope_null")

    payload = read_payload_or_none(row)
    if payload is None:
        return _row_fallback(row, source="fallback_decrypt_error")

    from_id = _coerce_uuid(payload.get("from_agent_id"))
    to_id = _coerce_uuid(payload.get("to_agent_id"))
    amount = _coerce_decimal(payload.get("amount"))
    description = payload.get("description")
    if description is not None and not isinstance(description, str):
        description = str(description)

    return ReadResult(
        from_agent_id=from_id,
        to_agent_id=to_id,
        amount=amount,
        description=description,
        source="envelope",
    )


# ---------------------------------------------------------------------------
# Repo helper: in-place row attribute swap
# ---------------------------------------------------------------------------


def apply_envelope_to_row(row: Any) -> ReadResult:
    """Mutate the ORM row's participant fields in place when envelope wins.

    This is the helper repos call after a query. When the flag is off or
    envelope can't be used, the row is left alone (original FK values
    untouched). When envelope wins, the row's ``from_agent_id`` /
    ``to_agent_id`` (or ``buyer_id`` / ``seller_id``), ``amount``, and
    ``memo`` / ``description`` get swapped. The returned ``ReadResult``
    lets the caller log or assert on the source path.

    Mutation is unavoidable here — SQLAlchemy ORM rows are reference-
    tracked by the session and we want callers to observe the new values
    transparently. This is read-only mutation: we never call ``session.add``
    so the change does not flush.
    """
    result = read_with_fallback(row)
    if result.source != "envelope":
        return result

    # Apply only to the columns the row exposes — keeps milestone (no
    # description column? — actually it has one) and message_relay (no
    # amount) safe.
    if hasattr(row, "from_agent_id"):
        row.from_agent_id = result.from_agent_id
    if hasattr(row, "to_agent_id"):
        row.to_agent_id = result.to_agent_id
    if hasattr(row, "buyer_id") and result.from_agent_id is not None:
        row.buyer_id = result.from_agent_id
    if hasattr(row, "seller_id") and result.to_agent_id is not None:
        row.seller_id = result.to_agent_id
    if hasattr(row, "amount") and result.amount is not None:
        row.amount = result.amount
    if hasattr(row, "memo"):
        row.memo = result.description
    elif hasattr(row, "description"):
        row.description = result.description

    return result
