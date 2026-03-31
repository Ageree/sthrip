"""Tests for fee collector service"""
import pytest
from decimal import Decimal
from sthrip.services.fee_collector import FeeCollector, FeeType, DEFAULT_FEES
from sthrip.db.models import HubRouteStatus


class TestFeeCalculation:
    def setup_method(self):
        self.collector = FeeCollector.__new__(FeeCollector)
        self.collector.db = None
        self.collector.fee_wallet_address = None

    def test_hub_routing_base_fee(self):
        result = self.collector.calculate_hub_routing_fee(Decimal("10.0"))
        assert result["fee_amount"] == Decimal("0.1")
        assert result["fee_percent"] == Decimal("0.01")
        assert result["recipient_receives"] == Decimal("10.0")
        assert result["total_deduction"] == Decimal("10.1")

    def test_hub_routing_min_fee(self):
        result = self.collector.calculate_hub_routing_fee(Decimal("0.001"))
        assert result["fee_amount"] == Decimal("0.0001")

    def test_hub_routing_no_cap(self):
        """Fee is always 1% with no practical cap."""
        result = self.collector.calculate_hub_routing_fee(Decimal("5000.0"))
        assert result["fee_amount"] == Decimal("50.0")

    def test_premium_tier_no_discount(self):
        """Flat 1% — no tier discounts."""
        normal = self.collector.calculate_hub_routing_fee(Decimal("100.0"), from_agent_tier="free")
        premium = self.collector.calculate_hub_routing_fee(Decimal("100.0"), from_agent_tier="premium")
        assert premium["fee_amount"] == normal["fee_amount"]

    def test_verified_tier_no_discount(self):
        """Flat 1% — no tier discounts."""
        normal = self.collector.calculate_hub_routing_fee(Decimal("100.0"), from_agent_tier="free")
        verified = self.collector.calculate_hub_routing_fee(Decimal("100.0"), from_agent_tier="verified")
        assert verified["fee_amount"] == normal["fee_amount"]

    def test_urgency_ignored(self):
        """Flat 1% — no urgency premium."""
        normal = self.collector.calculate_hub_routing_fee(Decimal("100.0"))
        urgent = self.collector.calculate_hub_routing_fee(Decimal("100.0"), urgency="urgent")
        assert urgent["fee_amount"] == normal["fee_amount"]

    def test_zero_amount_fee_capped_at_amount(self):
        """Fee for zero amount must be zero (fee cannot exceed payment amount)."""
        result = self.collector.calculate_hub_routing_fee(Decimal("0.0"))
        assert result["fee_amount"] == Decimal("0.0")

    def test_fee_config_values(self):
        hub = DEFAULT_FEES[FeeType.HUB_ROUTING]
        assert hub.percent == Decimal("0.01")
        assert hub.min_fee == Decimal("0.0001")
        assert hub.max_fee == Decimal("999999999")


# ─────────────────────────────────────────────────────────────────────────────
# Escrow fee calculation (lines 144-158)
# ─────────────────────────────────────────────────────────────────────────────

from unittest.mock import MagicMock, patch


class TestEscrowFeeCalculation:
    def setup_method(self):
        self.collector = FeeCollector.__new__(FeeCollector)
        self.collector.db = None
        self.collector.fee_wallet_address = None

    def test_escrow_fee_basic(self):
        result = self.collector.calculate_escrow_fee(Decimal("10.0"))
        assert result["escrow_amount"] == Decimal("10.0")
        assert result["fee_amount"] == Decimal("0.1")  # 1% of 10
        assert result["fee_percent"] == Decimal("0.01")
        assert result["tier_discount"] == "free"
        assert result["seller_receives"] == Decimal("9.9")

    def test_escrow_fee_premium_no_discount(self):
        """Flat 1% — no tier discounts."""
        result = self.collector.calculate_escrow_fee(Decimal("10.0"), from_agent_tier="premium")
        assert result["fee_amount"] == Decimal("0.1")  # No discount
        assert result["tier_discount"] == "premium"

    def test_escrow_fee_verified_no_discount(self):
        """Flat 1% — no tier discounts."""
        result = self.collector.calculate_escrow_fee(Decimal("10.0"), from_agent_tier="verified")
        assert result["fee_amount"] == Decimal("0.1")  # No discount
        assert result["tier_discount"] == "verified"

    def test_escrow_fee_min_applied(self):
        result = self.collector.calculate_escrow_fee(Decimal("0.01"))
        assert result["fee_amount"] == Decimal("0.0001")  # min_fee

    def test_escrow_fee_config(self):
        config = DEFAULT_FEES[FeeType.ESCROW]
        assert config.percent == Decimal("0.01")
        assert config.min_fee == Decimal("0.0001")
        assert config.max_fee == Decimal("999999999")


# ─────────────────────────────────────────────────────────────────────────────
# settle_hub_route (lines 304-316)
# ─────────────────────────────────────────────────────────────────────────────


class TestConfirmHubRoute:
    @patch("sthrip.services.fee_collector.get_db")
    def test_confirm_uses_for_update(self, mock_get_db):
        """confirm_hub_route must use FOR UPDATE to prevent double fee collection."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_route = MagicMock()
        mock_route.status = HubRouteStatus.PENDING
        mock_route.fee_amount = Decimal("0.01")
        mock_route.token = "XMR"
        mock_route.confirmed_at = None

        query_chain = mock_db.query.return_value.filter.return_value
        query_chain.with_for_update.return_value.first.return_value = mock_route

        collector = FeeCollector()
        result = collector.confirm_hub_route("hp_test")

        # Verify FOR UPDATE was called
        query_chain.with_for_update.assert_called_once()
        assert result["status"] == "confirmed"

    @patch("sthrip.services.fee_collector.get_db")
    def test_double_confirm_raises(self, mock_get_db):
        """Second confirmation of same route must raise ValueError."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_route = MagicMock()
        mock_route.status = HubRouteStatus.CONFIRMED

        query_chain = mock_db.query.return_value.filter.return_value
        query_chain.with_for_update.return_value.first.return_value = mock_route

        collector = FeeCollector()
        with pytest.raises(ValueError, match="already confirmed"):
            collector.confirm_hub_route("hp_test")


class TestSettleHubRoute:
    @patch("sthrip.services.fee_collector.get_db")
    def test_settle_existing_route(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_route = MagicMock()
        mock_route.status = HubRouteStatus.CONFIRMED
        mock_route.settled_at = None
        mock_filter = mock_db.query.return_value.filter.return_value
        mock_filter.with_for_update.return_value.first.return_value = mock_route

        collector = FeeCollector()
        result = collector.settle_hub_route("hp_abc", "tx_hash_123")

        assert result["payment_id"] == "hp_abc"
        assert result["status"] == "settled"
        assert result["settlement_tx"] == "tx_hash_123"

    @patch("sthrip.services.fee_collector.get_db")
    def test_settle_uses_for_update_and_orm_mutation(self, mock_get_db):
        """settle_hub_route must use with_for_update() and set attributes on ORM object."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_route = MagicMock()
        mock_route.status = HubRouteStatus.CONFIRMED
        mock_filter = mock_db.query.return_value.filter.return_value
        mock_filter.with_for_update.return_value.first.return_value = mock_route

        collector = FeeCollector()
        collector.settle_hub_route("hp_abc", "tx_hash_123")

        # Verify with_for_update was used
        mock_filter.with_for_update.assert_called_once()
        # Verify ORM attributes were set
        assert mock_route.status == HubRouteStatus.SETTLED
        assert mock_route.settlement_tx_hash == "tx_hash_123"

    @patch("sthrip.services.fee_collector.get_db")
    def test_settle_nonexistent_route(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)
        mock_db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = None

        collector = FeeCollector()
        with pytest.raises(ValueError, match="Route not found"):
            collector.settle_hub_route("hp_nonexistent", "tx_hash")


# ─────────────────────────────────────────────────────────────────────────────
# get_revenue_stats (lines 325-354)
# ─────────────────────────────────────────────────────────────────────────────


class TestGetRevenueStats:
    @patch("sthrip.services.fee_collector.get_db")
    def test_returns_stats(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_filter = MagicMock()
        mock_query.filter.return_value = mock_filter
        mock_filter.scalar.return_value = Decimal("1.5")
        mock_filter.count.return_value = 10

        collector = FeeCollector()
        result = collector.get_revenue_stats(days=30)

        assert result["period_days"] == 30
        assert "hub_routing_revenue_xmr" in result
        assert "escrow_revenue_xmr" in result
        assert "api_calls_revenue_usd" in result
        assert "total_routes" in result
        assert "average_fee_per_route" in result

    @patch("sthrip.services.fee_collector.get_db")
    def test_zero_routes_no_division_error(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_filter = MagicMock()
        mock_query.filter.return_value = mock_filter
        mock_filter.scalar.return_value = None  # No revenue
        mock_filter.count.return_value = 0       # No routes

        collector = FeeCollector()
        result = collector.get_revenue_stats(days=7)

        assert result["total_routes"] == 0
        assert result["average_fee_per_route"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# get_pending_fees (lines 365-371)
# ─────────────────────────────────────────────────────────────────────────────


class TestGetPendingFees:
    @patch("sthrip.services.fee_collector.get_db")
    def test_returns_pending_fees_list(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_fee = MagicMock()
        mock_fee.id = "fee_1"
        mock_fee.source_type = "hub_routing"
        mock_fee.amount = Decimal("0.05")
        mock_fee.token = "XMR"
        mock_fee.created_at.isoformat.return_value = "2026-03-09T00:00:00"

        mock_db.query.return_value.filter.return_value.all.return_value = [mock_fee]

        collector = FeeCollector()
        result = collector.get_pending_fees(token="XMR")

        assert len(result) == 1
        assert result[0]["id"] == "fee_1"
        assert result[0]["amount"] == "0.05"
        assert result[0]["token"] == "XMR"

    @patch("sthrip.services.fee_collector.get_db")
    def test_empty_pending_fees(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_db.query.return_value.filter.return_value.all.return_value = []

        collector = FeeCollector()
        result = collector.get_pending_fees()
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# withdraw_fees (lines 384-398)
# ─────────────────────────────────────────────────────────────────────────────


class TestWithdrawFees:
    @patch("sthrip.services.fee_collector.get_db")
    def test_withdraw_pending_fees(self, mock_get_db):
        """Bulk withdraw should return count and total using FOR UPDATE locking."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        fee1 = MagicMock(amount=Decimal("0.1"))
        fee2 = MagicMock(amount=Decimal("0.1"))
        mock_db.query.return_value.filter.return_value.with_for_update.return_value.all.return_value = [fee1, fee2]

        collector = FeeCollector()
        result = collector.withdraw_fees(["fee_1", "fee_2"], "tx_abc")

        assert result["withdrawn_fees"] == 2
        assert result["total_amount"] == "0.2"
        assert result["tx_hash"] == "tx_abc"

    @patch("sthrip.services.fee_collector.get_db")
    def test_withdraw_skips_nonpending(self, mock_get_db):
        """Bulk withdraw with no pending fees should return 0 count and 0 total."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_db.query.return_value.filter.return_value.with_for_update.return_value.all.return_value = []

        collector = FeeCollector()
        result = collector.withdraw_fees(["fee_1"], "tx_abc")

        assert result["total_amount"] == "0"
        assert result["withdrawn_fees"] == 0

    @patch("sthrip.services.fee_collector.get_db")
    def test_withdraw_missing_fee(self, mock_get_db):
        """Bulk withdraw with nonexistent IDs should return 0 count."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_filter = MagicMock()
        mock_query.filter.return_value = mock_filter
        mock_filter.scalar.return_value = Decimal("0")
        mock_filter.update.return_value = 0

        collector = FeeCollector()
        result = collector.withdraw_fees(["nonexistent"], "tx_abc")

        assert result["total_amount"] == "0"
        assert result["withdrawn_fees"] == 0
