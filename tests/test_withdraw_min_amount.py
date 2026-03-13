"""Test that withdrawal requests enforce a minimum amount."""
import pytest
from decimal import Decimal
from pydantic import ValidationError
from api.schemas import WithdrawRequest


# Valid stagenet address for tests
VALID_ADDR = "5" + "A" * 94


def test_withdraw_rejects_dust_amount():
    """Amounts below 0.001 XMR must be rejected."""
    with pytest.raises(ValidationError) as exc_info:
        WithdrawRequest(amount=Decimal("0.0001"), address=VALID_ADDR)
    assert "amount" in str(exc_info.value).lower()


def test_withdraw_accepts_minimum_amount():
    """Exactly 0.001 XMR should be accepted."""
    req = WithdrawRequest(amount=Decimal("0.001"), address=VALID_ADDR)
    assert req.amount == Decimal("0.001")


def test_withdraw_accepts_normal_amount():
    """Normal amounts should work fine."""
    req = WithdrawRequest(amount=Decimal("1.5"), address=VALID_ADDR)
    assert req.amount == Decimal("1.5")


def test_withdraw_rejects_zero():
    """Zero amount must be rejected."""
    with pytest.raises(ValidationError):
        WithdrawRequest(amount=Decimal("0"), address=VALID_ADDR)


def test_withdraw_rejects_negative():
    """Negative amount must be rejected."""
    with pytest.raises(ValidationError):
        WithdrawRequest(amount=Decimal("-1"), address=VALID_ADDR)
