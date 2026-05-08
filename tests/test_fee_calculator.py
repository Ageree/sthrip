"""Phase 2 Sprint 2 — unit tests for sthrip.services.fee_calculator.

Covers contract criteria 1-6 (per-tier rate, dust floor, no-float, integer return).
"""
from __future__ import annotations

import pytest

from sthrip.db.enums import AgentTier
from sthrip.services.fee_calculator import (
    BPS_DENOMINATOR,
    FREE_TIER_RATE_BPS,
    MIN_FEE_PICONERO,
    PRO_TIER_RATE_BPS,
    compute_fee,
    rate_bps_for_tier,
)


class TestComputeFee:
    def test_free_tier_pays_03_percent(self):
        # 0.1 XMR (100_000_000 piconero) → 0.0003 XMR (300_000 piconero)
        assert compute_fee(100_000_000, AgentTier.FREE) == 300_000

    def test_pro_tier_pays_01_percent(self):
        # AgentTier.VERIFIED maps to Pro rate.
        assert compute_fee(100_000_000, AgentTier.VERIFIED) == 100_000

    def test_premium_tier_pays_01_percent(self):
        assert compute_fee(100_000_000, AgentTier.PREMIUM) == 100_000

    def test_enterprise_tier_pays_01_percent(self):
        # Same rate as Pro.
        assert compute_fee(100_000_000, AgentTier.ENTERPRISE) == 100_000

    def test_dust_transfer_pays_floor(self):
        # 100 piconero * 0.003 = 0.3 → rounded to 0 → floored to 1.
        assert compute_fee(100, AgentTier.FREE) == 1

    def test_zero_amount_pays_floor(self):
        # Even zero pays the minimum — keeps fee_collections row count
        # equal to transaction count for trivially-correct revenue MTD.
        assert compute_fee(0, AgentTier.FREE) == MIN_FEE_PICONERO

    def test_no_float_arithmetic(self):
        # Result must be int (not Decimal, not float).
        result = compute_fee(123_456_789, AgentTier.FREE)
        assert isinstance(result, int)
        assert not isinstance(result, bool)

    def test_negative_amount_rejected(self):
        with pytest.raises(ValueError):
            compute_fee(-1, AgentTier.FREE)

    def test_non_int_amount_rejected(self):
        with pytest.raises(TypeError):
            compute_fee(100.0, AgentTier.FREE)  # type: ignore[arg-type]

    def test_large_amount_no_overflow(self):
        # 21_000_000 XMR * 10**12 piconero is the universe-cap monero supply.
        # Verify the math doesn't overflow.
        amount = 21_000_000 * 10**12
        result = compute_fee(amount, AgentTier.FREE)
        # 0.3% of supply
        assert result == amount * 30 // 10_000

    def test_round_half_up(self):
        # 333 * 30 / 10_000 = 0.999 → round half-up → 1
        assert compute_fee(333, AgentTier.FREE) == 1

    def test_constants_match_contract(self):
        assert FREE_TIER_RATE_BPS == 30
        assert PRO_TIER_RATE_BPS == 10
        assert MIN_FEE_PICONERO == 1
        assert BPS_DENOMINATOR == 10_000


class TestRateLookup:
    def test_free_rate(self):
        assert rate_bps_for_tier(AgentTier.FREE) == FREE_TIER_RATE_BPS

    def test_verified_rate(self):
        assert rate_bps_for_tier(AgentTier.VERIFIED) == PRO_TIER_RATE_BPS

    def test_premium_rate(self):
        assert rate_bps_for_tier(AgentTier.PREMIUM) == PRO_TIER_RATE_BPS

    def test_enterprise_rate(self):
        assert rate_bps_for_tier(AgentTier.ENTERPRISE) == PRO_TIER_RATE_BPS
