"""Tests for fee collector service"""
import pytest
from decimal import Decimal
from stealthpay.services.fee_collector import FeeCollector, FeeType, DEFAULT_FEES


class TestFeeCalculation:
    def setup_method(self):
        self.collector = FeeCollector.__new__(FeeCollector)
        self.collector.db = None
        self.collector.fee_wallet_address = None

    def test_hub_routing_base_fee(self):
        result = self.collector.calculate_hub_routing_fee(Decimal("10.0"))
        assert result["fee_amount"] == Decimal("0.01")
        assert result["fee_percent"] == Decimal("0.001")
        assert result["recipient_receives"] == Decimal("10.0")
        assert result["total_deduction"] == Decimal("10.01")

    def test_hub_routing_min_fee(self):
        result = self.collector.calculate_hub_routing_fee(Decimal("0.001"))
        assert result["fee_amount"] == Decimal("0.0001")

    def test_hub_routing_max_fee(self):
        result = self.collector.calculate_hub_routing_fee(Decimal("5000.0"))
        assert result["fee_amount"] == Decimal("1.0")

    def test_premium_tier_discount(self):
        normal = self.collector.calculate_hub_routing_fee(Decimal("100.0"), from_agent_tier="free")
        premium = self.collector.calculate_hub_routing_fee(Decimal("100.0"), from_agent_tier="premium")
        assert premium["fee_amount"] == normal["fee_amount"] * Decimal("0.5")

    def test_verified_tier_discount(self):
        normal = self.collector.calculate_hub_routing_fee(Decimal("100.0"), from_agent_tier="free")
        verified = self.collector.calculate_hub_routing_fee(Decimal("100.0"), from_agent_tier="verified")
        assert verified["fee_amount"] == normal["fee_amount"] * Decimal("0.75")

    def test_urgent_doubles_fee(self):
        normal = self.collector.calculate_hub_routing_fee(Decimal("100.0"))
        urgent = self.collector.calculate_hub_routing_fee(Decimal("100.0"), urgency="urgent")
        assert urgent["fee_amount"] == normal["fee_amount"] * Decimal("2.0")

    def test_zero_amount_hits_min_fee(self):
        result = self.collector.calculate_hub_routing_fee(Decimal("0.0"))
        assert result["fee_amount"] == Decimal("0.0001")

    def test_fee_config_values(self):
        hub = DEFAULT_FEES[FeeType.HUB_ROUTING]
        assert hub.percent == Decimal("0.001")
        assert hub.min_fee == Decimal("0.0001")
        assert hub.max_fee == Decimal("1.0")
