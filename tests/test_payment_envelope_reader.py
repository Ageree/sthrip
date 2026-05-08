"""Sprint 4a: unit tests for payment_envelope_reader.

The reader module is the dual-read counterpart to the Sprint 3 writer.
Behaviour matrix (controlled by env var ``STHRIP_READ_FROM_ENVELOPE``):

| Flag | Envelope     | Decrypt | Result source              |
|------|--------------|---------|----------------------------|
| off  | n/a          | n/a     | flag_off                   |
| on   | null         | n/a     | fallback_envelope_null     |
| on   | present      | ok      | envelope                   |
| on   | present      | fails   | fallback_decrypt_error     |

These tests use lightweight stand-in objects (no SQLAlchemy session) to
keep the reader's surface honest.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID, uuid4

import pytest

from sthrip.services import payment_envelope_reader as reader
from sthrip.services.envelope_crypto import (
    PaymentEnvelope,
    encrypt_envelope,
    load_hub_kek,
)
from sthrip.services.operator_keystore import get_keystore


# ---------------------------------------------------------------------------
# Stand-in row class — mimics the parts of an ORM Transaction we care about
# ---------------------------------------------------------------------------


@dataclass
class FakeTxRow:
    from_agent_id: Optional[UUID]
    to_agent_id: Optional[UUID]
    amount: Optional[Decimal]
    memo: Optional[str]
    participant_envelope: Optional[bytes] = None


@dataclass
class FakeEscrowRow:
    buyer_id: Optional[UUID]
    seller_id: Optional[UUID]
    amount: Optional[Decimal]
    description: Optional[str]
    participant_envelope: Optional[bytes] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope_blob(
    *,
    from_id: UUID,
    to_id: UUID,
    amount: Decimal,
    description: Optional[str],
) -> bytes:
    """Encrypt a payment payload using the test KEKs."""
    hub_kek = load_hub_kek()
    op_kek = get_keystore().get_kek_for_envelope()
    env = encrypt_envelope(
        {
            "from_agent_id": str(from_id),
            "to_agent_id": str(to_id),
            "amount": str(amount),
            "description": description,
        },
        hub_kek,
        op_kek,
    )
    return env.to_bytes()


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


def test_feature_flag_default_false(monkeypatch):
    """Default behaviour: flag is off, reads use FKs."""
    monkeypatch.delenv("STHRIP_READ_FROM_ENVELOPE", raising=False)
    assert reader.feature_flag_enabled() is False


@pytest.mark.parametrize("value,expected", [
    ("1", True),
    ("true", True),
    ("TRUE", True),
    ("yes", True),
    ("on", True),
    ("0", False),
    ("false", False),
    ("", False),
    ("nope", False),
])
def test_feature_flag_truthy_values(monkeypatch, value, expected):
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", value)
    assert reader.feature_flag_enabled() is expected


# ---------------------------------------------------------------------------
# read_with_fallback — flag off
# ---------------------------------------------------------------------------


def test_read_with_fallback_flag_off_uses_fk_only(monkeypatch):
    """Flag off: even if envelope is present, reader returns FK values."""
    monkeypatch.delenv("STHRIP_READ_FROM_ENVELOPE", raising=False)

    fk_from = uuid4()
    fk_to = uuid4()
    env_from = uuid4()
    env_to = uuid4()

    row = FakeTxRow(
        from_agent_id=fk_from,
        to_agent_id=fk_to,
        amount=Decimal("5"),
        memo="fk-memo",
        participant_envelope=_make_envelope_blob(
            from_id=env_from, to_id=env_to,
            amount=Decimal("99"), description="env-desc",
        ),
    )
    result = reader.read_with_fallback(row)
    assert result.source == "flag_off"
    assert result.from_agent_id == fk_from
    assert result.to_agent_id == fk_to
    assert result.amount == Decimal("5")
    assert result.description == "fk-memo"


# ---------------------------------------------------------------------------
# read_with_fallback — flag on, envelope null
# ---------------------------------------------------------------------------


def test_read_with_fallback_envelope_null_falls_back_to_fk(monkeypatch):
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")
    fk_from = uuid4()
    fk_to = uuid4()
    row = FakeTxRow(
        from_agent_id=fk_from,
        to_agent_id=fk_to,
        amount=Decimal("3"),
        memo="legacy",
        participant_envelope=None,
    )
    result = reader.read_with_fallback(row)
    assert result.source == "fallback_envelope_null"
    assert result.from_agent_id == fk_from
    assert result.to_agent_id == fk_to


# ---------------------------------------------------------------------------
# read_with_fallback — flag on, envelope present, decrypt OK
# ---------------------------------------------------------------------------


def test_read_with_fallback_envelope_present_flag_on(monkeypatch):
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")
    fk_from = uuid4()
    fk_to = uuid4()
    env_from = uuid4()
    env_to = uuid4()
    blob = _make_envelope_blob(
        from_id=env_from, to_id=env_to,
        amount=Decimal("12.5"), description="from-envelope",
    )
    row = FakeTxRow(
        from_agent_id=fk_from,
        to_agent_id=fk_to,
        amount=Decimal("999"),  # FK garbage — must NOT be used
        memo="fk-memo",
        participant_envelope=blob,
    )
    result = reader.read_with_fallback(row)
    assert result.source == "envelope"
    assert result.from_agent_id == env_from
    assert result.to_agent_id == env_to
    assert result.amount == Decimal("12.5")
    assert result.description == "from-envelope"


# ---------------------------------------------------------------------------
# read_with_fallback — flag on, envelope corrupt
# ---------------------------------------------------------------------------


def test_read_with_fallback_decrypt_fails_falls_back_to_fk(monkeypatch, caplog):
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")
    fk_from = uuid4()
    fk_to = uuid4()
    row = FakeTxRow(
        from_agent_id=fk_from,
        to_agent_id=fk_to,
        amount=Decimal("7"),
        memo="legacy-memo",
        participant_envelope=b"this-is-not-a-valid-envelope-blob",
    )
    with caplog.at_level("WARNING", logger="sthrip.services.payment_envelope_reader"):
        result = reader.read_with_fallback(row)
    assert result.source == "fallback_decrypt_error"
    assert result.from_agent_id == fk_from
    assert result.to_agent_id == fk_to
    assert result.amount == Decimal("7")
    # Warning was emitted (we do not assert on raw bytes content of the log)
    assert any("decrypt failed" in rec.message for rec in caplog.records)


def test_read_with_fallback_wrong_key_falls_back(monkeypatch):
    """Envelope encrypted with a different KEK (key rotation simulated)."""
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")

    # Build an envelope with a DIFFERENT hub KEK than the one in env.
    other_hub_kek = b"\x11" * 32
    op_kek = get_keystore().get_kek_for_envelope()
    env = encrypt_envelope(
        {
            "from_agent_id": str(uuid4()),
            "to_agent_id": str(uuid4()),
            "amount": "1",
            "description": "desc",
        },
        other_hub_kek,
        op_kek,
    )
    fk_from = uuid4()
    fk_to = uuid4()
    row = FakeTxRow(
        from_agent_id=fk_from,
        to_agent_id=fk_to,
        amount=Decimal("4"),
        memo="legacy",
        participant_envelope=env.to_bytes(),
    )
    result = reader.read_with_fallback(row)
    assert result.source == "fallback_decrypt_error"
    # Fallback honours the FK values
    assert result.from_agent_id == fk_from


# ---------------------------------------------------------------------------
# Escrow rows (buyer_id / seller_id, description)
# ---------------------------------------------------------------------------


def test_read_with_fallback_escrow_buyer_seller(monkeypatch):
    """Escrow rows expose buyer_id/seller_id, not from/to."""
    monkeypatch.delenv("STHRIP_READ_FROM_ENVELOPE", raising=False)
    buyer = uuid4()
    seller = uuid4()
    row = FakeEscrowRow(
        buyer_id=buyer,
        seller_id=seller,
        amount=Decimal("250"),
        description="research",
    )
    result = reader.read_with_fallback(row)
    assert result.from_agent_id == buyer
    assert result.to_agent_id == seller
    assert result.amount == Decimal("250")
    assert result.description == "research"


# ---------------------------------------------------------------------------
# read_payload_or_none
# ---------------------------------------------------------------------------


def test_read_payload_or_none_with_no_envelope():
    row = FakeTxRow(
        from_agent_id=uuid4(),
        to_agent_id=uuid4(),
        amount=Decimal("1"),
        memo=None,
        participant_envelope=None,
    )
    assert reader.read_payload_or_none(row) is None


def test_read_payload_or_none_with_corrupt_envelope():
    row = FakeTxRow(
        from_agent_id=uuid4(),
        to_agent_id=uuid4(),
        amount=Decimal("1"),
        memo=None,
        participant_envelope=b"\x00\x00\x00garbage",
    )
    assert reader.read_payload_or_none(row) is None


def test_read_payload_or_none_with_valid_envelope():
    a, b = uuid4(), uuid4()
    blob = _make_envelope_blob(
        from_id=a, to_id=b, amount=Decimal("2"), description="ok",
    )
    row = FakeTxRow(
        from_agent_id=None, to_agent_id=None,
        amount=None, memo=None, participant_envelope=blob,
    )
    out = reader.read_payload_or_none(row)
    assert out is not None
    assert out["from_agent_id"] == str(a)
    assert out["to_agent_id"] == str(b)
    assert out["amount"] == "2"


# ---------------------------------------------------------------------------
# apply_envelope_to_row — in-place mutation
# ---------------------------------------------------------------------------


def test_apply_envelope_to_row_no_op_when_flag_off(monkeypatch):
    monkeypatch.delenv("STHRIP_READ_FROM_ENVELOPE", raising=False)
    fk_from = uuid4()
    row = FakeTxRow(
        from_agent_id=fk_from,
        to_agent_id=uuid4(),
        amount=Decimal("1"),
        memo="orig",
    )
    result = reader.apply_envelope_to_row(row)
    assert result.source == "flag_off"
    # Row untouched
    assert row.from_agent_id == fk_from
    assert row.memo == "orig"


def test_apply_envelope_to_row_swaps_when_envelope_wins(monkeypatch):
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")
    env_from = uuid4()
    env_to = uuid4()
    blob = _make_envelope_blob(
        from_id=env_from, to_id=env_to,
        amount=Decimal("33"), description="env-memo",
    )
    row = FakeTxRow(
        from_agent_id=uuid4(),
        to_agent_id=uuid4(),
        amount=Decimal("999"),
        memo="legacy",
        participant_envelope=blob,
    )
    result = reader.apply_envelope_to_row(row)
    assert result.source == "envelope"
    assert row.from_agent_id == env_from
    assert row.to_agent_id == env_to
    assert row.amount == Decimal("33")
    assert row.memo == "env-memo"


def test_apply_envelope_to_row_no_swap_on_decrypt_fail(monkeypatch):
    monkeypatch.setenv("STHRIP_READ_FROM_ENVELOPE", "true")
    fk_from = uuid4()
    row = FakeTxRow(
        from_agent_id=fk_from,
        to_agent_id=uuid4(),
        amount=Decimal("1"),
        memo="legacy",
        participant_envelope=b"corrupt",
    )
    result = reader.apply_envelope_to_row(row)
    assert result.source == "fallback_decrypt_error"
    # Row preserved
    assert row.from_agent_id == fk_from
    assert row.memo == "legacy"
