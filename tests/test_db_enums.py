"""
Tests for sthrip.db.enums module.

TDD RED phase: these tests fail until sthrip/db/enums.py is created and
sthrip/db/models.py re-exports every enum for backward compatibility.
"""

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_ENUM_NAMES = [
    "PrivacyLevel",
    "AgentTier",
    "RateLimitTier",
    "TransactionStatus",
    "PaymentType",
    "EscrowStatus",
    "ChannelStatus",
    "WebhookStatus",
    "HubRouteStatus",
    "FeeCollectionStatus",
]


# ---------------------------------------------------------------------------
# 1. Import surface: sthrip.db.enums must expose all enums
# ---------------------------------------------------------------------------

class TestEnumsModuleExists:
    """sthrip.db.enums must be importable and expose all enum classes."""

    def test_enums_module_is_importable(self):
        import sthrip.db.enums  # noqa: F401

    def test_all_enum_names_present_in_enums_module(self):
        import sthrip.db.enums as enums_mod
        for name in ALL_ENUM_NAMES:
            assert hasattr(enums_mod, name), (
                f"sthrip.db.enums is missing '{name}'"
            )

    def test_enums_are_enum_subclasses(self):
        import sthrip.db.enums as enums_mod
        from enum import Enum
        for name in ALL_ENUM_NAMES:
            cls = getattr(enums_mod, name)
            assert issubclass(cls, Enum), (
                f"sthrip.db.enums.{name} is not an Enum subclass"
            )

    def test_enums_are_str_subclasses(self):
        """All enums must also subclass str for SQLAlchemy compatibility."""
        import sthrip.db.enums as enums_mod
        for name in ALL_ENUM_NAMES:
            cls = getattr(enums_mod, name)
            assert issubclass(cls, str), (
                f"sthrip.db.enums.{name} does not subclass str"
            )


# ---------------------------------------------------------------------------
# 2. Backward compatibility: sthrip.db.models must still expose all enums
# ---------------------------------------------------------------------------

class TestBackwardCompatModels:
    """Existing code imports enums from sthrip.db.models — must keep working."""

    def test_all_enum_names_present_in_models_module(self):
        import sthrip.db.models as models_mod
        for name in ALL_ENUM_NAMES:
            assert hasattr(models_mod, name), (
                f"sthrip.db.models no longer exports '{name}' (backward compat broken)"
            )

    def test_models_and_enums_expose_same_class_objects(self):
        """The class objects must be identical (not just same name) to avoid
        SQLAlchemy column type mismatches."""
        import sthrip.db.enums as enums_mod
        import sthrip.db.models as models_mod
        for name in ALL_ENUM_NAMES:
            assert getattr(models_mod, name) is getattr(enums_mod, name), (
                f"sthrip.db.models.{name} is not the same object as "
                f"sthrip.db.enums.{name}"
            )


# ---------------------------------------------------------------------------
# 3. PrivacyLevel members
# ---------------------------------------------------------------------------

class TestPrivacyLevel:
    def test_has_expected_members(self):
        from sthrip.db.enums import PrivacyLevel
        assert PrivacyLevel.LOW.value == "low"
        assert PrivacyLevel.MEDIUM.value == "medium"
        assert PrivacyLevel.HIGH.value == "high"
        assert PrivacyLevel.PARANOID.value == "paranoid"

    def test_member_count(self):
        from sthrip.db.enums import PrivacyLevel
        assert len(PrivacyLevel) == 4

    def test_string_comparison(self):
        from sthrip.db.enums import PrivacyLevel
        assert PrivacyLevel.LOW == "low"
        assert PrivacyLevel.PARANOID == "paranoid"


# ---------------------------------------------------------------------------
# 4. AgentTier members
# ---------------------------------------------------------------------------

class TestAgentTier:
    def test_has_expected_members(self):
        from sthrip.db.enums import AgentTier
        assert AgentTier.FREE.value == "free"
        assert AgentTier.VERIFIED.value == "verified"
        assert AgentTier.PREMIUM.value == "premium"
        assert AgentTier.ENTERPRISE.value == "enterprise"

    def test_member_count(self):
        from sthrip.db.enums import AgentTier
        assert len(AgentTier) == 4

    def test_string_comparison(self):
        from sthrip.db.enums import AgentTier
        assert AgentTier.FREE == "free"
        assert AgentTier.ENTERPRISE == "enterprise"


# ---------------------------------------------------------------------------
# 5. RateLimitTier members
# ---------------------------------------------------------------------------

class TestRateLimitTier:
    def test_has_expected_members(self):
        from sthrip.db.enums import RateLimitTier
        assert RateLimitTier.LOW.value == "low"
        assert RateLimitTier.STANDARD.value == "standard"
        assert RateLimitTier.HIGH.value == "high"
        assert RateLimitTier.UNLIMITED.value == "unlimited"

    def test_member_count(self):
        from sthrip.db.enums import RateLimitTier
        assert len(RateLimitTier) == 4

    def test_string_comparison(self):
        from sthrip.db.enums import RateLimitTier
        assert RateLimitTier.STANDARD == "standard"
        assert RateLimitTier.UNLIMITED == "unlimited"


# ---------------------------------------------------------------------------
# 6. TransactionStatus members
# ---------------------------------------------------------------------------

class TestTransactionStatus:
    def test_has_expected_members(self):
        from sthrip.db.enums import TransactionStatus
        assert TransactionStatus.PENDING.value == "pending"
        assert TransactionStatus.CONFIRMED.value == "confirmed"
        assert TransactionStatus.FAILED.value == "failed"
        assert TransactionStatus.ORPHANED.value == "orphaned"

    def test_member_count(self):
        from sthrip.db.enums import TransactionStatus
        assert len(TransactionStatus) == 4


# ---------------------------------------------------------------------------
# 7. PaymentType members
# ---------------------------------------------------------------------------

class TestPaymentType:
    def test_has_expected_members(self):
        from sthrip.db.enums import PaymentType
        assert PaymentType.P2P.value == "p2p"
        assert PaymentType.HUB_ROUTING.value == "hub_routing"
        assert PaymentType.ESCROW_DEPOSIT.value == "escrow_deposit"
        assert PaymentType.ESCROW_RELEASE.value == "escrow_release"
        assert PaymentType.CHANNEL_OPEN.value == "channel_open"
        assert PaymentType.CHANNEL_CLOSE.value == "channel_close"
        assert PaymentType.FEE_COLLECTION.value == "fee_collection"

    def test_member_count(self):
        from sthrip.db.enums import PaymentType
        assert len(PaymentType) == 9


# ---------------------------------------------------------------------------
# 8. EscrowStatus members
# ---------------------------------------------------------------------------

class TestEscrowStatus:
    def test_has_expected_members(self):
        from sthrip.db.enums import EscrowStatus
        assert EscrowStatus.PENDING.value == "pending"
        assert EscrowStatus.FUNDED.value == "funded"
        assert EscrowStatus.DELIVERED.value == "delivered"
        assert EscrowStatus.COMPLETED.value == "completed"
        assert EscrowStatus.DISPUTED.value == "disputed"
        assert EscrowStatus.REFUNDED.value == "refunded"
        assert EscrowStatus.EXPIRED.value == "expired"

    def test_member_count(self):
        from sthrip.db.enums import EscrowStatus
        assert len(EscrowStatus) == 7


# ---------------------------------------------------------------------------
# 9. ChannelStatus members
# ---------------------------------------------------------------------------

class TestChannelStatus:
    def test_has_expected_members(self):
        from sthrip.db.enums import ChannelStatus
        assert ChannelStatus.PENDING.value == "pending"
        assert ChannelStatus.OPEN.value == "open"
        assert ChannelStatus.CLOSING.value == "closing"
        assert ChannelStatus.CLOSED.value == "closed"
        assert ChannelStatus.DISPUTED.value == "disputed"

    def test_member_count(self):
        from sthrip.db.enums import ChannelStatus
        assert len(ChannelStatus) == 5


# ---------------------------------------------------------------------------
# 10. WebhookStatus members
# ---------------------------------------------------------------------------

class TestWebhookStatus:
    def test_has_expected_members(self):
        from sthrip.db.enums import WebhookStatus
        assert WebhookStatus.PENDING.value == "pending"
        assert WebhookStatus.DELIVERED.value == "delivered"
        assert WebhookStatus.FAILED.value == "failed"
        assert WebhookStatus.RETRYING.value == "retrying"

    def test_member_count(self):
        from sthrip.db.enums import WebhookStatus
        assert len(WebhookStatus) == 4


# ---------------------------------------------------------------------------
# 11. HubRouteStatus members
# ---------------------------------------------------------------------------

class TestHubRouteStatus:
    def test_has_expected_members(self):
        from sthrip.db.enums import HubRouteStatus
        assert HubRouteStatus.PENDING.value == "pending"
        assert HubRouteStatus.CONFIRMED.value == "confirmed"
        assert HubRouteStatus.SETTLED.value == "settled"
        assert HubRouteStatus.FAILED.value == "failed"

    def test_member_count(self):
        from sthrip.db.enums import HubRouteStatus
        assert len(HubRouteStatus) == 4


# ---------------------------------------------------------------------------
# 12. FeeCollectionStatus members
# ---------------------------------------------------------------------------

class TestFeeCollectionStatus:
    def test_has_expected_members(self):
        from sthrip.db.enums import FeeCollectionStatus
        assert FeeCollectionStatus.PENDING.value == "pending"
        assert FeeCollectionStatus.COLLECTED.value == "collected"
        assert FeeCollectionStatus.WITHDRAWN.value == "withdrawn"

    def test_member_count(self):
        from sthrip.db.enums import FeeCollectionStatus
        assert len(FeeCollectionStatus) == 3


# ---------------------------------------------------------------------------
# 13. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_enum_values_are_lowercase_strings(self):
        """All enum string values must be lowercase (SQL convention)."""
        import sthrip.db.enums as enums_mod
        for name in ALL_ENUM_NAMES:
            cls = getattr(enums_mod, name)
            for member in cls:
                assert member.value == member.value.lower(), (
                    f"{name}.{member.name}.value '{member.value}' is not lowercase"
                )

    def test_enum_names_are_uppercase(self):
        """All enum member names must be uppercase (Python convention)."""
        import sthrip.db.enums as enums_mod
        for name in ALL_ENUM_NAMES:
            cls = getattr(enums_mod, name)
            for member in cls:
                assert member.name == member.name.upper(), (
                    f"{name} member '{member.name}' is not uppercase"
                )

    def test_no_duplicate_values_within_enum(self):
        """Each enum must have unique values (no aliases unless intentional)."""
        import sthrip.db.enums as enums_mod
        from enum import unique
        for name in ALL_ENUM_NAMES:
            cls = getattr(enums_mod, name)
            values = [m.value for m in cls]
            assert len(values) == len(set(values)), (
                f"{name} has duplicate values: {values}"
            )

    def test_enum_iteration_order_is_stable(self):
        """Iterating an enum twice should return members in the same order."""
        import sthrip.db.enums as enums_mod
        for name in ALL_ENUM_NAMES:
            cls = getattr(enums_mod, name)
            first_pass = list(cls)
            second_pass = list(cls)
            assert first_pass == second_pass, (
                f"{name} iteration order is not stable"
            )

    def test_enum_lookup_by_value(self):
        """Enums must be constructable from their string value."""
        from sthrip.db.enums import (
            PrivacyLevel, AgentTier, TransactionStatus, WebhookStatus
        )
        assert PrivacyLevel("low") is PrivacyLevel.LOW
        assert AgentTier("premium") is AgentTier.PREMIUM
        assert TransactionStatus("confirmed") is TransactionStatus.CONFIRMED
        assert WebhookStatus("retrying") is WebhookStatus.RETRYING

    def test_unknown_value_raises_value_error(self):
        """Constructing an enum with an unknown value must raise ValueError."""
        from sthrip.db.enums import AgentTier
        with pytest.raises(ValueError):
            AgentTier("does_not_exist")

    def test_enum_repr_contains_class_name(self):
        """repr() of an enum member should include its class name."""
        from sthrip.db.enums import PrivacyLevel
        assert "PrivacyLevel" in repr(PrivacyLevel.HIGH)
