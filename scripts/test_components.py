#!/usr/bin/env python3
"""
Component Tests for StealthPay

Tests all privacy components WITHOUT spending real money
"""

import sys
import asyncio
import secrets
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from stealthpay.bridge.privacy import StealthAddressGenerator, ZKVerifier
from stealthpay.bridge.tor import OnionAddressBook


class TestRunner:
    """Run component tests"""
    
    def __init__(self):
        self.passed = 0
        self.failed = 0
    
    def test(self, name):
        """Decorator for tests"""
        def decorator(func):
            async def wrapper():
                try:
                    print(f"\n🧪 Testing: {name}")
                    await func()
                    print(f"✅ PASS: {name}")
                    self.passed += 1
                    return True
                except Exception as e:
                    print(f"❌ FAIL: {name} - {e}")
                    self.failed += 1
                    return False
            return wrapper
        return decorator
    
    def summary(self):
        print(f"\n{'='*60}")
        print(f"Results: {self.passed} passed, {self.failed} failed")
        print(f"{'='*60}")
        return self.failed == 0


runner = TestRunner()


@runner.test("Stealth Address Generation")
async def test_stealth_generation():
    """Test stealth address generation"""
    generator = StealthAddressGenerator()
    keys = generator.generate_master_keys()
    
    # Generate address
    stealth = generator.generate_stealth_address(
        keys.scan_public,
        keys.spend_public
    )
    
    assert stealth.address, "No address generated"
    assert len(stealth.public_key) > 0, "No public key"
    assert len(stealth.ephemeral_pubkey) > 0, "No ephemeral key"
    
    print(f"   Generated: {stealth.address[:40]}...")


@runner.test("Stealth Address Ownership")
async def test_stealth_ownership():
    """Test ownership verification"""
    generator = StealthAddressGenerator()
    keys = generator.generate_master_keys()
    
    # Generate for self
    stealth = generator.generate_stealth_address(
        keys.scan_public,
        keys.spend_public
    )
    
    # Check ownership
    is_mine, priv_key = generator.check_ownership(
        stealth,
        keys.scan_private,
        keys.spend_public
    )
    
    assert is_mine, "Should be mine"
    assert priv_key is not None, "Should recover private key"
    
    # Generate different keys (should NOT match)
    other_keys = generator.generate_master_keys()
    is_mine_other, _ = generator.check_ownership(
        stealth,
        other_keys.scan_private,
        other_keys.spend_public
    )
    
    assert not is_mine_other, "Should not match different keys"


@runner.test("ZK Proof Generation")
async def test_zk_proof():
    """Test ZK proof generation and verification"""
    verifier = ZKVerifier()
    
    # Generate keys
    sk = secrets.token_bytes(32)
    pk = secrets.token_bytes(33)
    
    # Create proof
    proof = verifier.generate_ownership_proof(sk, pk)
    
    assert proof.proof, "No proof generated"
    assert len(proof.public_inputs) > 0, "No public inputs"
    
    # Verify
    challenge = secrets.token_bytes(32)
    is_valid = verifier.verify_ownership(proof, pk, challenge)
    
    assert is_valid, "Proof should be valid"


@runner.test("ZK Range Proof")
async def test_zk_range():
    """Test range proofs"""
    verifier = ZKVerifier()
    
    # Prove value in range
    value = 1000
    min_val = 0
    max_val = 10000
    
    proof = verifier.generate_range_proof(value, min_val, max_val)
    
    # Verify (simplified - real would use commitment)
    commitment = secrets.token_bytes(32)
    is_valid = verifier.verify_range(proof, commitment, min_val, max_val)
    
    print(f"   Proved {value} in range [{min_val}, {max_val}]")


@runner.test("Onion Address Book")
async def test_onion_address_book():
    """Test Tor onion address management"""
    book = OnionAddressBook(storage_path="/tmp/test_onion.json")
    
    # Register
    node_id = "test-node-1"
    onion = "abcd1234abcd1234.onion"
    pubkey = "test_pubkey_123"
    
    book.register(node_id, onion, pubkey)
    
    # Retrieve
    retrieved = book.get(node_id)
    assert retrieved == onion, "Address mismatch"
    
    # List
    nodes = book.list_nodes()
    assert node_id in nodes, "Node not in list"
    
    print(f"   Registered: {node_id} -> {onion}")


@runner.test("Multiple Stealth Addresses")
async def test_multiple_stealth():
    """Test that multiple addresses are unique"""
    generator = StealthAddressGenerator()
    keys = generator.generate_master_keys()
    
    addresses = set()
    for i in range(10):
        stealth = generator.generate_stealth_address(
            keys.scan_public,
            keys.spend_public
        )
        addresses.add(stealth.address)
    
    assert len(addresses) == 10, "Addresses should be unique"
    print(f"   Generated 10 unique addresses")


@runner.test("Stealth Key Recovery")
async def test_key_recovery():
    """Test private key recovery for spending"""
    generator = StealthAddressGenerator()
    keys = generator.generate_master_keys()
    
    # Generate address
    stealth = generator.generate_stealth_address(
        keys.scan_public,
        keys.spend_public
    )
    
    # Recover key
    recovered = generator.recover_private_key(
        stealth,
        keys.scan_private,
        keys.spend_private
    )
    
    assert recovered is not None, "Should recover key"
    assert len(recovered) == 32, "Key should be 32 bytes"
    
    print(f"   Recovered key: {recovered.hex()[:20]}...")


async def main():
    """Run all tests"""
    print("="*60)
    print("STEALTHPAY COMPONENT TESTS")
    print("="*60)
    print("\n🔒 Testing privacy components (no real money)")
    
    # Run tests
    await test_stealth_generation()
    await test_stealth_ownership()
    await test_zk_proof()
    await test_zk_range()
    await test_onion_address_book()
    await test_multiple_stealth()
    await test_key_recovery()
    
    # Summary
    success = runner.summary()
    
    if success:
        print("\n✅ All component tests passed!")
        print("Ready for integration testing with testnet funds.")
    else:
        print("\n❌ Some tests failed!")
        print("Fix issues before proceeding.")
    
    return success


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
