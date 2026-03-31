"""Tests for flat 1% fee model — no tier discounts."""

from decimal import Decimal

import pytest

from sthrip.services.fee_collector import FeeCollector, FeeType, DEFAULT_FEES


def test_hub_routing_fee_is_1_percent():
    collector = FeeCollector()
    result = collector.calculate_hub_routing_fee(amount=Decimal("10.0"))
    assert result["fee_percent"] == Decimal("0.01")
    assert result["fee_amount"] == Decimal("0.1")


def test_hub_routing_fee_no_tier_discount_for_premium():
    collector = FeeCollector()
    result = collector.calculate_hub_routing_fee(
        amount=Decimal("10.0"), from_agent_tier="premium"
    )
    assert result["fee_percent"] == Decimal("0.01")
    assert result["fee_amount"] == Decimal("0.1")


def test_hub_routing_fee_no_tier_discount_for_verified():
    collector = FeeCollector()
    result = collector.calculate_hub_routing_fee(
        amount=Decimal("10.0"), from_agent_tier="verified"
    )
    assert result["fee_percent"] == Decimal("0.01")


def test_escrow_fee_is_1_percent():
    collector = FeeCollector()
    result = collector.calculate_escrow_fee(amount=Decimal("10.0"))
    assert result["fee_percent"] == Decimal("0.01")
    assert result["fee_amount"] == Decimal("0.1")
    assert result["seller_receives"] == Decimal("9.9")


def test_escrow_fee_no_tier_discount():
    collector = FeeCollector()
    result = collector.calculate_escrow_fee(
        amount=Decimal("10.0"), from_agent_tier="premium"
    )
    assert result["fee_percent"] == Decimal("0.01")


def test_default_fees_config():
    assert DEFAULT_FEES[FeeType.HUB_ROUTING].percent == Decimal("0.01")
    assert DEFAULT_FEES[FeeType.ESCROW].percent == Decimal("0.01")
    assert DEFAULT_FEES[FeeType.CROSS_CHAIN].percent == Decimal("0.01")
