"""
Tests for Bitcoin HTLC implementation
"""

import pytest
from decimal import Decimal
import hashlib

from sthrip.swaps.btc.htlc import (
    create_htlc_redeem_script,
    create_htlc_address,
    BitcoinHTLC,
    create_simple_htlc_for_swap
)
from sthrip.swaps.utils.bitcoin import sha256, hash160, encode_bech32, generate_keypair


class TestHTLCRedeemScript:
    """Tests for HTLC redeem script generation"""
    
    def test_create_htlc_redeem_script(self):
        """Тест создания HTLC redeem script"""
        preimage_hash = sha256(b"test preimage")
        sender_pubkey = b'\x02' + b'\x00' * 32  # Compressed pubkey
        recipient_pubkey = b'\x03' + b'\x00' * 32
        locktime = 100
        
        script = create_htlc_redeem_script(
            preimage_hash,
            recipient_pubkey,
            sender_pubkey,
            locktime
        )
        
        assert isinstance(script, bytes)
        assert len(script) > 50  # Script should be reasonably sized
        
        # Script should contain our hash
        assert preimage_hash in script
        
        # Script should contain pubkeys
        assert recipient_pubkey in script
        assert sender_pubkey in script
    
    def test_htlc_address_creation(self):
        """Тест создания HTLC адреса"""
        preimage_hash = sha256(b"test preimage")
        sender_pubkey = b'\x02' + b'\x00' * 32
        recipient_pubkey = b'\x03' + b'\x00' * 32
        locktime = 100
        
        address, redeem_script = create_htlc_address(
            preimage_hash,
            recipient_pubkey,
            sender_pubkey,
            locktime,
            network="testnet"
        )
        
        # Проверяем формат адреса
        assert address.startswith("tb1")  # Testnet bech32
        assert len(address) > 30
        
        # Redeem script должен быть валидным
        assert isinstance(redeem_script, bytes)
        assert len(redeem_script) > 0


class TestBitcoinHTLC:
    """Tests for BitcoinHTLC class"""
    
    def test_generate_preimage(self):
        """Тест генерации preimage"""
        # Mock RPC client
        class MockRPC:
            def get_block_count(self):
                return 1000
        
        htlc = BitcoinHTLC(MockRPC(), network="testnet")
        preimage = htlc.generate_preimage()
        
        assert isinstance(preimage, bytes)
        assert len(preimage) == 32
        
        # Hash should be generated
        assert htlc.preimage_hash is not None
        assert len(htlc.preimage_hash) == 32
        
        # Verify hash
        expected_hash = hashlib.sha256(preimage).digest()
        assert htlc.preimage_hash == expected_hash
    
    def test_create_htlc(self):
        """Тест создания HTLC контракта"""
        class MockRPC:
            def get_block_count(self):
                return 1000
        
        htlc = BitcoinHTLC(MockRPC(), network="testnet")
        
        sender_pubkey = b'\x02' + b'\x00' * 32
        recipient_pubkey = b'\x03' + b'\x00' * 32
        
        contract = htlc.create_htlc(
            sender_pubkey,
            recipient_pubkey,
            locktime_blocks=144,  # ~24 hours
            amount_btc=Decimal("0.01")
        )
        
        assert "address" in contract
        assert "redeem_script" in contract
        assert "preimage_hash" in contract
        assert "locktime" in contract
        
        # Locktime должен быть в будущем
        assert contract["locktime"] > 1000
        
        # Preimage должен быть сгенерирован
        assert "preimage" in contract
        assert len(contract["preimage"]) == 64  # hex


class TestUtils:
    """Tests for utility functions"""
    
    def test_sha256(self):
        """Тест SHA256"""
        data = b"hello"
        result = sha256(data)
        
        assert isinstance(result, bytes)
        assert len(result) == 32
        
        # Проверяем с известным значением
        expected = hashlib.sha256(data).digest()
        assert result == expected
    
    def test_hash160(self):
        """Тест hash160"""
        data = b"hello"
        result = hash160(data)
        
        assert isinstance(result, bytes)
        assert len(result) == 20
    
    def test_bech32_encode_decode(self):
        """Тест bech32 кодирования"""
        hrp = "tb"
        witver = 0
        witprog = b'\x00' * 20  # P2WPKH
        
        address = encode_bech32(hrp, witver, witprog)
        
        assert address.startswith("tb1")
        
        # Декодируем обратно
        from sthrip.swaps.utils.bitcoin import decode_bech32
        decoded_hrp, decoded_ver, decoded_prog = decode_bech32(address)
        
        assert decoded_hrp == hrp
        assert decoded_ver == witver
        assert decoded_prog == witprog
    
    def test_generate_keypair(self):
        """Тест генерации ключей"""
        privkey, pubkey = generate_keypair()
        
        assert isinstance(privkey, bytes)
        assert isinstance(pubkey, bytes)
        assert len(privkey) == 32
        assert len(pubkey) == 33  # Compressed
        
        # Pubkey должен начинаться с 0x02 или 0x03
        assert pubkey[0] in [0x02, 0x03]


class TestSwapFlow:
    """Integration tests for swap flow"""
    
    def test_simple_htlc_for_swap(self):
        """Тест создания HTLC для свопа"""
        class MockRPC:
            def get_block_count(self):
                return 1000
        
        sender_privkey, sender_pubkey = generate_keypair()
        recipient_privkey, recipient_pubkey = generate_keypair()
        
        contract = create_simple_htlc_for_swap(
            MockRPC(),
            sender_pubkey.hex(),
            recipient_pubkey.hex(),
            Decimal("0.01"),
            locktime_hours=24,
            network="testnet"
        )
        
        assert "address" in contract
        assert "redeem_script" in contract
        assert "preimage" in contract
        assert "preimage_hash" in contract
        
        # Проверяем что preimage соответствует hash
        preimage_bytes = bytes.fromhex(contract["preimage"])
        expected_hash = hashlib.sha256(preimage_bytes).hexdigest()
        assert contract["preimage_hash"] == expected_hash


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
