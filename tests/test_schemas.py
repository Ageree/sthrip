"""Tests for Pydantic request/response schemas."""

import os
from decimal import Decimal

import pytest


os.environ.setdefault("MONERO_NETWORK", "stagenet")


def test_withdraw_request_accepts_decimal_precision():
    """WithdrawRequest.amount must preserve Decimal precision."""
    from api.schemas import WithdrawRequest

    req = WithdrawRequest(amount=Decimal("1.123456789012"), address="5" + "a" * 94)
    assert isinstance(req.amount, Decimal)
    assert req.amount == Decimal("1.123456789012")


def test_payment_request_uses_decimal():
    """PaymentRequest.amount must be Decimal, not float."""
    from api.schemas import PaymentRequest

    req = PaymentRequest(to_address="5" + "a" * 94, amount=Decimal("0.5"))
    assert isinstance(req.amount, Decimal)


def test_hub_payment_request_uses_decimal():
    """HubPaymentRequest.amount must be Decimal."""
    from api.schemas import HubPaymentRequest

    req = HubPaymentRequest(to_agent_name="test-agent", amount=Decimal("1.0"))
    assert isinstance(req.amount, Decimal)


def test_deposit_request_uses_decimal():
    """DepositRequest.amount must be Decimal when provided."""
    from api.schemas import DepositRequest

    req = DepositRequest(amount=Decimal("5.0"))
    assert isinstance(req.amount, Decimal)


def test_float_input_coerced_to_decimal():
    """JSON float input must be coerced to Decimal by Pydantic."""
    from api.schemas import PaymentRequest

    req = PaymentRequest(to_address="5" + "a" * 94, amount=1.5)
    assert isinstance(req.amount, Decimal)


def test_escrow_create_request_uses_decimal():
    """EscrowCreateRequest.amount must be Decimal."""
    from api.schemas import EscrowCreateRequest

    req = EscrowCreateRequest(
        seller_address="5" + "a" * 94,
        amount=Decimal("2.0"),
        description="test escrow",
    )
    assert isinstance(req.amount, Decimal)


# ─────────────────────────────────────────────────────────────────────────────
# AgentSettingsUpdate: wallet address validation
# ─────────────────────────────────────────────────────────────────────────────

def test_settings_update_rejects_empty_wallet_addresses():
    """Empty string wallet addresses must be rejected."""
    from pydantic import ValidationError
    from api.schemas import AgentSettingsUpdate

    for field in ("xmr_address", "base_address", "solana_address"):
        with pytest.raises(ValidationError) as exc_info:
            AgentSettingsUpdate(**{field: ""})
        assert field in str(exc_info.value), f"Expected {field} validation error"


def test_settings_update_accepts_none_wallet_addresses():
    """None wallet addresses must be accepted (no-op)."""
    from api.schemas import AgentSettingsUpdate

    update = AgentSettingsUpdate(xmr_address=None, base_address=None, solana_address=None)
    assert update.xmr_address is None
    assert update.base_address is None
    assert update.solana_address is None


def test_settings_update_accepts_valid_wallet_addresses():
    """Non-empty wallet addresses must be accepted."""
    from api.schemas import AgentSettingsUpdate

    update = AgentSettingsUpdate(
        base_address="0x1234567890abcdef",
        solana_address="So11111111111111111111111111111111111111112",
    )
    assert update.base_address == "0x1234567890abcdef"
    assert update.solana_address == "So11111111111111111111111111111111111111112"
