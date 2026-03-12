"""Test that mark_used creates a new StealthAddress, not mutating the old one."""
from unittest.mock import MagicMock
from sthrip.stealth import StealthAddressManager
from sthrip.types import StealthAddress


def test_mark_used_does_not_mutate_original():
    wallet = MagicMock()
    wallet.get_address_index.return_value = {"index": {"minor": 1}}

    mgr = StealthAddressManager(wallet)
    original = StealthAddress(address="addr_1", index=1, label="test", used=False)
    mgr._cache[1] = original

    # Keep a reference to the original object
    original_ref = mgr._cache[1]

    mgr.mark_used("addr_1")

    # The cache should have a NEW object with used=True
    assert mgr._cache[1].used is True
    # The original reference should NOT be mutated
    assert original_ref.used is False, "Original object was mutated instead of replaced"
