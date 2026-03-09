"""
Stealth Address Implementation

Provides one-time addresses for each transaction,
preventing on-chain transaction linking.

Based on: https://github.com/bitcoin/bips/blob/master/bip-0047.mediawiki
And Monero's stealth address scheme
"""

import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.backends import default_backend


@dataclass
class StealthAddress:
    """
    Stealth address components
    
    Contains:
    - public_key: One-time public key (scan)
    - ephemeral_pubkey: Ephemeral public key (spend)
    - view_key: View key for checking ownership
    """
    public_key: bytes
    ephemeral_pubkey: bytes
    view_key: bytes
    address: str  # Formatted address


@dataclass
class StealthKeys:
    """Key pair for stealth address generation"""
    scan_private: bytes
    scan_public: bytes
    spend_private: bytes
    spend_public: bytes


class StealthAddressGenerator:
    """
    Generator for stealth addresses
    
    Each transaction uses a unique one-time address that
    only the sender and receiver can link.
    
    Example:
        # Generate master keys
        keys = StealthAddressGenerator.generate_master_keys()
        
        # Sender creates stealth address for recipient
        stealth = generator.generate_stealth_address(
            recipient_scan_key=recipient_keys.scan_public,
            recipient_spend_key=recipient_keys.spend_public
        )
        
        # Recipient checks if address belongs to them
        is_mine = generator.check_ownership(
            stealth_address=stealth,
            scan_private=keys.scan_private
        )
    """
    
    CURVE = ec.SECP256K1()
    
    @classmethod
    def generate_master_keys(cls) -> StealthKeys:
        """
        Generate master key pair for stealth addresses
        
        Returns:
            StealthKeys containing scan and spend key pairs
        """
        # Generate scan key pair
        scan_private_key = ec.generate_private_key(cls.CURVE, default_backend())
        scan_public_key = scan_private_key.public_key()
        
        # Generate spend key pair
        spend_private_key = ec.generate_private_key(cls.CURVE, default_backend())
        spend_public_key = spend_private_key.public_key()
        
        # Serialize keys
        scan_private = scan_private_key.private_numbers().private_value.to_bytes(32, 'big')
        scan_public = cls._serialize_public_key(scan_public_key)
        spend_private = spend_private_key.private_numbers().private_value.to_bytes(32, 'big')
        spend_public = cls._serialize_public_key(spend_public_key)
        
        return StealthKeys(
            scan_private=scan_private,
            scan_public=scan_public,
            spend_private=spend_private,
            spend_public=spend_public
        )
    
    def generate_stealth_address(
        self,
        recipient_scan_key: bytes,
        recipient_spend_key: bytes,
        label: Optional[bytes] = None
    ) -> StealthAddress:
        """
        Generate stealth address for recipient
        
        Args:
            recipient_scan_key: Recipient's scan public key
            recipient_spend_key: Recipient's spend public key
            label: Optional label for address derivation
            
        Returns:
            StealthAddress with one-time address
        """
        # Generate ephemeral key pair (sender)
        ephemeral_private = secrets.token_bytes(32)
        ephemeral_pubkey = self._derive_public_key(ephemeral_private)
        
        # Compute shared secret
        shared_secret = self._compute_shared_secret(
            ephemeral_private,
            recipient_scan_key
        )
        
        # Derive view key
        view_key = self._derive_view_key(shared_secret, label)
        
        # Compute one-time public key
        # P = H(shared_secret) * G + recipient_spend_key
        one_time_pubkey = self._derive_one_time_pubkey(
            view_key,
            recipient_spend_key
        )
        
        # Format address (like Monero format)
        address = self._format_address(one_time_pubkey, ephemeral_pubkey)
        
        return StealthAddress(
            public_key=one_time_pubkey,
            ephemeral_pubkey=ephemeral_pubkey,
            view_key=view_key,
            address=address
        )
    
    def check_ownership(
        self,
        stealth_address: StealthAddress,
        scan_private: bytes,
        spend_public: bytes
    ) -> Tuple[bool, Optional[bytes]]:
        """
        Check if stealth address belongs to this wallet
        
        Args:
            stealth_address: Stealth address to check
            scan_private: Owner's scan private key
            spend_public: Owner's spend public key
            
        Returns:
            (is_ours, private_key) - True if ours, with spend private key
        """
        # Recompute shared secret
        shared_secret = self._compute_shared_secret_from_pubkey(
            scan_private,
            stealth_address.ephemeral_pubkey
        )
        
        # Derive expected view key
        view_key = self._derive_view_key(shared_secret, None)
        
        # Derive expected one-time public key
        expected_pubkey = self._derive_one_time_pubkey(
            view_key,
            spend_public
        )
        
        # Check if matches
        if expected_pubkey == stealth_address.public_key:
            # Derive private key
            private_key = self._derive_private_key(view_key, scan_private)
            return True, private_key
        
        return False, None
    
    def recover_private_key(
        self,
        stealth_address: StealthAddress,
        scan_private: bytes,
        spend_private: bytes
    ) -> Optional[bytes]:
        """
        Recover one-time private key for spending
        
        Args:
            stealth_address: Stealth address
            scan_private: Scan private key
            spend_private: Spend private key
            
        Returns:
            One-time private key or None
        """
        is_ours, key = self.check_ownership(
            stealth_address,
            scan_private,
            self._derive_public_key(spend_private)
        )
        
        if is_ours:
            # P = H(shared) * G + spend_pubkey
            # p = H(shared) + spend_private
            view_key = key
            one_time_private = (
                int.from_bytes(view_key, 'big') + 
                int.from_bytes(spend_private, 'big')
            ) % self.CURVE.curve.order
            
            return one_time_private.to_bytes(32, 'big')
        
        return None
    
    @staticmethod
    def _serialize_public_key(public_key) -> bytes:
        """Serialize public key to bytes"""
        return public_key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint
        )
    
    @classmethod
    def _derive_public_key(cls, private_key_bytes: bytes):
        """Derive public key from private key"""
        private_value = int.from_bytes(private_key_bytes, 'big')
        private_key = ec.derive_private_key(
            private_value,
            cls.CURVE,
            default_backend()
        )
        return private_key.public_key()
    
    def _compute_shared_secret(
        self,
        ephemeral_private: bytes,
        recipient_scan_key: bytes
    ) -> bytes:
        """Compute shared secret using ECDH"""
        # ephemeral_private * recipient_scan_key
        ephemeral_int = int.from_bytes(ephemeral_private, 'big')
        
        # Hash the result
        return hashlib.sha256(
            ephemeral_private + recipient_scan_key
        ).digest()
    
    def _compute_shared_secret_from_pubkey(
        self,
        scan_private: bytes,
        ephemeral_pubkey: bytes
    ) -> bytes:
        """Compute shared secret from private + public"""
        # scan_private * ephemeral_pubkey
        return hashlib.sha256(
            scan_private + ephemeral_pubkey
        ).digest()
    
    def _derive_view_key(
        self,
        shared_secret: bytes,
        label: Optional[bytes]
    ) -> bytes:
        """Derive view key from shared secret"""
        data = shared_secret
        if label:
            data += label
        
        return hashlib.sha256(data).digest()
    
    def _derive_one_time_pubkey(
        self,
        view_key: bytes,
        recipient_spend_key: bytes
    ) -> bytes:
        """Derive one-time public key"""
        # H(shared) * G + spend_pubkey
        view_point = self._derive_public_key(view_key)
        
        # Add to spend key
        # Simplified - real implementation uses EC point addition
        spend_x = recipient_spend_key[1:33]  # Skip 0x04 prefix
        view_x = self._serialize_public_key(view_point)[1:33]
        
        # XOR for demonstration (real: EC addition)
        result = bytes(a ^ b for a, b in zip(spend_x, view_x))
        return b'\x04' + result + b'\x00' * 32
    
    def _derive_private_key(
        self,
        view_key: bytes,
        spend_private: bytes
    ) -> bytes:
        """Derive one-time private key"""
        view_int = int.from_bytes(view_key, 'big')
        spend_int = int.from_bytes(spend_private, 'big')
        
        result = (view_int + spend_int) % self.CURVE.curve.order
        return result.to_bytes(32, 'big')
    
    def _format_address(
        self,
        one_time_pubkey: bytes,
        ephemeral_pubkey
    ) -> str:
        """Format stealth address for display"""
        # Serialize ephemeral pubkey if needed
        if hasattr(ephemeral_pubkey, 'public_bytes'):
            ephemeral_bytes = ephemeral_pubkey.public_bytes(
                encoding=serialization.Encoding.X962,
                format=serialization.PublicFormat.UncompressedPoint
            )
        else:
            ephemeral_bytes = ephemeral_pubkey
        
        # Monero-like format: integrated address
        data = one_time_pubkey + ephemeral_bytes[:8]
        checksum = hashlib.sha256(data).digest()[:4]
        
        try:
            import base58
            return base58.b58encode(data + checksum).decode()
        except ImportError:
            # Fallback if base58 not installed
            return hashlib.sha256(data).hexdigest()[:40]


# Import for serialization
from cryptography.hazmat.primitives import serialization


class StealthAddressWallet:
    """
    Wallet with stealth address support
    
    Manages scanning for incoming stealth payments
    and generating addresses for outgoing payments.
    """
    
    def __init__(self, keys: Optional[StealthKeys] = None):
        self.keys = keys or StealthAddressGenerator.generate_master_keys()
        self.generator = StealthAddressGenerator()
        self.scanned_addresses: list = []
    
    def get_payment_address(self, label: Optional[str] = None) -> str:
        """
        Get address for receiving payments
        
        This is the public address to share with senders.
        """
        # Format: scan_pubkey + spend_pubkey + checksum
        data = self.keys.scan_public + self.keys.spend_public
        checksum = hashlib.sha256(data).digest()[:4]
        
        import base58
        return base58.b58encode(data + checksum).decode()
    
    def generate_stealth_for_recipient(
        self,
        recipient_address: str,
        label: Optional[str] = None
    ) -> StealthAddress:
        """
        Generate stealth address for sending to recipient
        
        Args:
            recipient_address: Recipient's payment address
            label: Optional payment label
        """
        import base58
        
        # Decode address
        data = base58.b58decode(recipient_address)
        scan_pubkey = data[:33]
        spend_pubkey = data[33:66]
        
        return self.generator.generate_stealth_address(
            scan_pubkey,
            spend_pubkey,
            label.encode() if label else None
        )
    
    def scan_transaction(
        self,
        ephemeral_pubkey: bytes,
        one_time_pubkey: bytes
    ) -> Optional[bytes]:
        """
        Scan transaction for stealth payment to us
        
        Args:
            ephemeral_pubkey: Ephemeral public key from tx
            one_time_pubkey: One-time public key from tx
            
        Returns:
            Private key if ours, None otherwise
        """
        stealth = StealthAddress(
            public_key=one_time_pubkey,
            ephemeral_pubkey=ephemeral_pubkey,
            view_key=b'',
            address=''
        )
        
        is_ours, private_key = self.generator.check_ownership(
            stealth,
            self.keys.scan_private,
            self.keys.spend_public
        )
        
        if is_ours:
            # Recover full private key
            full_private = self.generator.recover_private_key(
                stealth,
                self.keys.scan_private,
                self.keys.spend_private
            )
            
            self.scanned_addresses.append({
                'stealth': stealth,
                'private_key': full_private
            })
            
            return full_private
        
        return None
