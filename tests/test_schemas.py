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
