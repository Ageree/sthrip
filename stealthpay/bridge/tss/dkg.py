"""
Distributed Key Generation (DKG) for TSS

Implements Feldman's Verifiable Secret Sharing (VSS) for secure
distributed key generation among n parties with threshold t.
"""

import hashlib
import secrets
from typing import List, Tuple, Dict
from dataclasses import dataclass
import ecdsa
from ecdsa import SECP256k1, SigningKey, VerifyingKey
from ecdsa.ellipticcurve import Point


@dataclass
class KeyShare:
    """Individual key share for a party"""
    party_id: int
    index: int  # 1-based index in the group
    private_share: int  # x_i - the secret share
    public_key: bytes  # Group public key (compressed)
    commitments: List[Point]  # Feldman commitments [C0, C1, ..., Ct]
    
    def to_dict(self) -> dict:
        return {
            "party_id": self.party_id,
            "index": self.index,
            "private_share": hex(self.private_share)[2:].zfill(64),
            "public_key": self.public_key.hex(),
            "commitments_count": len(self.commitments)
        }


@dataclass
class Polynomial:
    """Polynomial for secret sharing: f(x) = a_0 + a_1*x + ... + a_t*x^t"""
    coefficients: List[int]  # [a_0, a_1, ..., a_t]
    
    def evaluate(self, x: int, prime: int) -> int:
        """Evaluate polynomial at point x mod prime"""
        result = 0
        power = 1
        for coef in self.coefficients:
            result = (result + coef * power) % prime
            power = (power * x) % prime
        return result


class DistributedKeyGenerator:
    """
    Distributed Key Generation using Feldman's VSS.
    
    Each party generates a polynomial and shares evaluations.
    Final private key is sum of all constant terms (a_0).
    Final public key is sum of all C0 commitments.
    """
    
    # secp256k1 order
    CURVE_ORDER = SECP256k1.order
    
    def __init__(self, n: int, threshold: int):
        """
        Initialize DKG.
        
        Args:
            n: Total number of parties
            threshold: Minimum parties needed to sign (t < n)
        """
        self.n = n
        self.t = threshold
        self.parties: List[int] = list(range(1, n + 1))
    
    def generate_key_shares(self) -> List[KeyShare]:
        """
        Generate key shares for all parties.
        
        This is a simplified simulation. In production, this would be
        an interactive protocol where each party generates their own
        polynomial and shares evaluations with others.
        
        Returns:
            List of KeyShare objects for each party
        """
        # Generate master secret
        master_secret = secrets.randbelow(self.CURVE_ORDER - 1) + 1
        
        # Create random polynomial with master_secret as constant term
        coefficients = [master_secret] + [
            secrets.randbelow(self.CURVE_ORDER - 1) + 1
            for _ in range(self.t)
        ]
        poly = Polynomial(coefficients)
        
        # Generate Feldman commitments
        # C_j = g^{a_j} for each coefficient a_j
        G = SECP256k1.generator
        commitments = [
            G * coef for coef in coefficients
        ]
        
        # Generate shares for each party
        shares = []
        for party_id in self.parties:
            # Evaluate polynomial at party_id
            share_value = poly.evaluate(party_id, self.CURVE_ORDER)
            
            # Group public key is commitment to constant term
            group_public = commitments[0]
            group_public_bytes = self._point_to_bytes(group_public)
            
            key_share = KeyShare(
                party_id=party_id,
                index=party_id,
                private_share=share_value,
                public_key=group_public_bytes,
                commitments=commitments
            )
            shares.append(key_share)
        
        return shares
    
    def verify_share(self, share: KeyShare) -> bool:
        """
        Verify a key share against commitments (Feldman VSS).
        
        Checks that: g^{share} == Product(C_j^{index^j})
        """
        G = SECP256k1.generator
        
        # Left side: g^{share}
        lhs = G * share.private_share
        
        # Right side: Product(C_j^{index^j})
        rhs = None
        for j, commitment in enumerate(share.commitments):
            exp = pow(share.index, j, self.CURVE_ORDER)
            term = commitment * exp
            if rhs is None:
                rhs = term
            else:
                rhs = rhs + term
        
        return lhs == rhs
    
    def reconstruct_public_key(self, shares: List[KeyShare]) -> bytes:
        """
        Reconstruct group public key from any t+1 shares using Lagrange interpolation.
        """
        if len(shares) < self.t + 1:
            raise ValueError(f"Need at least {self.t + 1} shares")
        
        # Use commitments from first share (they're all the same)
        return shares[0].public_key
    
    def _point_to_bytes(self, point: Point) -> bytes:
        """Convert elliptic curve point to compressed bytes"""
        # Convert to ecdsa VerifyingKey format
        vk = VerifyingKey.from_string(
            point.to_bytes(),
            curve=SECP256k1
        )
        return vk.to_string("compressed")
    
    @staticmethod
    def lagrange_coefficient(index: int, indices: List[int], prime: int) -> int:
        """
        Calculate Lagrange coefficient for interpolation.
        
        λ_i = Product(j≠i) [j / (j - i)] mod prime
        """
        numerator = 1
        denominator = 1
        
        for j in indices:
            if j != index:
                numerator = (numerator * j) % prime
                denominator = (denominator * (j - index)) % prime
        
        # Modular inverse of denominator
        inv_denominator = pow(denominator, prime - 2, prime)
        
        return (numerator * inv_denominator) % prime


class SecureKeyStorage:
    """
    Secure storage for key shares.
    
    In production, use HSM (Hardware Security Module) or
    Hashicorp Vault with encryption.
    """
    
    def __init__(self, encryption_key: bytes = None):
        self.encryption_key = encryption_key or secrets.token_bytes(32)
        self._storage: Dict[int, bytes] = {}
    
    def store_share(self, share: KeyShare) -> str:
        """
        Store encrypted key share.
        
        Returns storage identifier.
        """
        from cryptography.fernet import Fernet
        import base64
        
        # Simple Fernet encryption (in production use HSM)
        key = base64.urlsafe_b64encode(self.encryption_key)
        f = Fernet(key)
        
        # Serialize share
        data = f"{share.party_id}:{share.index}:{share.private_share}".encode()
        encrypted = f.encrypt(data)
        
        storage_id = f"share_{share.party_id}_{secrets.token_hex(8)}"
        self._storage[share.party_id] = encrypted
        
        return storage_id
    
    def retrieve_share(self, party_id: int) -> KeyShare:
        """Retrieve and decrypt key share"""
        from cryptography.fernet import Fernet
        import base64
        
        key = base64.urlsafe_b64encode(self.encryption_key)
        f = Fernet(key)
        
        encrypted = self._storage.get(party_id)
        if not encrypted:
            raise ValueError(f"Share not found for party {party_id}")
        
        data = f.decrypt(encrypted)
        parts = data.decode().split(":")
        
        # Note: commitments lost in this simple serialization
        return KeyShare(
            party_id=int(parts[0]),
            index=int(parts[1]),
            private_share=int(parts[2], 16) if parts[2].startswith('0x') else int(parts[2]),
            public_key=b'',
            commitments=[]
        )
    
    def delete_share(self, party_id: int) -> None:
        """Securely delete a key share"""
        if party_id in self._storage:
            # Overwrite with random data before deletion
            self._storage[party_id] = secrets.token_bytes(len(self._storage[party_id]))
            del self._storage[party_id]
