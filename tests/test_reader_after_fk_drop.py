"""Sprint 4b unit tests: payment_envelope_reader copes with dropped FK columns.

Once the Sprint 4b destructive migration runs, the payment-graph rows no
longer carry plaintext FK columns at all — only ``participant_envelope``.
The reader must access fields via ``getattr`` and gracefully handle
missing attributes.
"""
from __future__ import annotations

import os
import secrets
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sthrip.services import payment_envelope_reader as reader_module
from sthrip.services.envelope_crypto import encrypt_envelope, load_hub_kek
from sthrip.services.operator_keystore import StubKeystore, get_keystore
from sthrip.services.payment_envelope_reader import (
    ReadResult,
    apply_envelope_to_row,
    read_with_fallback,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_keystore(monkeypatch):
    """Force stub mode + fixed hub KEK for the whole module."""
    monkeypatch.setenv("OP_KEYSTORE_MODE", "stub")
    monkeypatch.setenv(
        "STHRIP_HUB_KEK", "00" * 32  # 32 zero bytes hex-encoded
    )
    get_keystore.cache_clear()
    yield
    get_keystore.cache_clear()


def _make_envelope_blob(*, from_id, to_id, amount: Decimal, description: str) -> bytes:
    hub_kek = load_hub_kek()
    op_kek = StubKeystore().get_kek_for_envelope()
    env = encrypt_envelope(
        {
            "from_agent_id": str(from_id),
            "to_agent_id": str(to_id),
            "amount": amount,
            "description": description,
        },
        hub_kek,
        op_kek,
    )
    return env.to_bytes()


# ---------------------------------------------------------------------------
# Reader tolerates missing FK columns
# ---------------------------------------------------------------------------


def test_reader_handles_missing_fk_columns_via_getattr(monkeypatch):
    """A row stripped of every legacy FK column still resolves through
    the envelope when the flag is on."""
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")

    from_id = uuid4()
    to_id = uuid4()
    blob = _make_envelope_blob(
        from_id=from_id, to_id=to_id, amount=Decimal("1.5"), description="post-drop"
    )

    # Row has ONLY participant_envelope — like a Sprint 4b post-cutover row.
    row = SimpleNamespace(participant_envelope=blob)

    result = read_with_fallback(row)
    assert result.source == "envelope"
    assert str(result.from_agent_id) == str(from_id)
    assert str(result.to_agent_id) == str(to_id)
    assert result.amount == Decimal("1.5")
    assert result.description == "post-drop"


def test_reader_returns_envelope_when_fk_dropped(monkeypatch):
    """Same as above but with description provided via the memo column."""
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")

    blob = _make_envelope_blob(
        from_id=uuid4(),
        to_id=uuid4(),
        amount=Decimal("0.001"),
        description="memo-shaped",
    )
    row = SimpleNamespace(participant_envelope=blob)

    result = read_with_fallback(row)
    assert result.source == "envelope"
    assert result.description == "memo-shaped"


def test_reader_no_envelope_no_fk_returns_fallback_no_data(monkeypatch):
    """Sprint 4b: row with no envelope AND no plaintext columns surfaces
    as ``fallback_no_data`` instead of pretending the empty result is a
    successful flag-off read."""
    monkeypatch.delenv("STHRIP_READ_FROM_ENVELOPE", raising=False)

    row = SimpleNamespace()  # no fields at all
    result = read_with_fallback(row)
    assert result.source == "fallback_no_data"
    assert result.from_agent_id is None
    assert result.to_agent_id is None
    assert result.amount is None
    assert result.description is None


def test_reader_envelope_null_no_fk_returns_fallback_no_data(monkeypatch):
    """Flag is on, envelope is None, FK columns missing — we cannot
    surface anything, but we should label it correctly."""
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")

    row = SimpleNamespace(participant_envelope=None)
    result = read_with_fallback(row)
    assert result.source == "fallback_no_data"


def test_apply_envelope_to_row_noop_on_legacy_columns_missing(monkeypatch):
    """``apply_envelope_to_row`` swaps fields only on attributes the row
    exposes — it never raises when legacy columns are missing."""
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")

    blob = _make_envelope_blob(
        from_id=uuid4(),
        to_id=uuid4(),
        amount=Decimal("9.99"),
        description="ok",
    )
    row = SimpleNamespace(participant_envelope=blob)
    # Should not raise even though the row has no FK columns to mutate.
    result = apply_envelope_to_row(row)
    assert result.source == "envelope"
    # Row remains a SimpleNamespace; we never created spurious attributes.
    assert not hasattr(row, "from_agent_id")
    assert not hasattr(row, "amount")


def test_reader_decrypt_error_with_no_fk_returns_fallback_no_data(monkeypatch):
    """Envelope present but corrupt + no FK columns → ``fallback_no_data``."""
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")

    row = SimpleNamespace(participant_envelope=b"this is not a valid envelope")
    result = read_with_fallback(row)
    # No FK columns to fall back to, so we surface no_data not decrypt_error.
    assert result.source == "fallback_no_data"


def test_reader_decrypt_error_with_fk_uses_fk_fallback(monkeypatch):
    """Envelope corrupt but legacy FK columns still present (transitional
    schema during cutover) → reader returns the FK fallback."""
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")

    row = SimpleNamespace(
        participant_envelope=b"corrupt",
        from_agent_id="legacy-from",
        to_agent_id="legacy-to",
        amount=Decimal("3.14"),
        memo="legacy-memo",
    )
    result = read_with_fallback(row)
    assert result.source == "fallback_decrypt_error"
    assert result.from_agent_id == "legacy-from"
    assert result.to_agent_id == "legacy-to"
    assert result.amount == Decimal("3.14")
