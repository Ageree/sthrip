"""
Signature Aggregator for Threshold ECDSA

Aggregates partial signatures into full ECDSA signatures.
"""

import hashlib
from typing import List
from dataclasses import dataclass
from ecdsa import SECP256k1, VerifyingKey
from ecdsa.ellipticcurve import Point
from ecdsa.util import sigencode_der, sigdecode_der

from .signer import PartialSignature, SigningContext
from .dkg import DistributedKeyGenerator


@dataclass
class ECDSASignature:
    """Full ECDSA signature (r, s)"""
    r: int
    s: int
    
    def to_der(self) -> bytes:
        """Convert to DER format"""
        from ecdsa.util import sigencode_der
        return sigencode_der(self.r, self.s, SECP256k1.order)
    
    def to_hex(self) -> str:
        """Convert to hex string"""
        return self.to_der().hex()
    
    @classmethod
    def from_der(cls, der_bytes: bytes) -> "ECDSASignature":
        """Parse from DER format"""
        from ecdsa.util import sigdecode_der
        r, s = sigdecode_der(der_bytes, SECP256k1.order)
        return cls(r, s)
    
    def verify(self, message_hash: bytes, public_key: bytes) -> bool:
        """Verify signature against public key"""
        try:
            vk = VerifyingKey.from_string(public_key, curve=SECP256k1)
            return vk.verify_digest(
                self.to_der(),
                message_hash,
                sigdecode=sigdecode_der
            )
        except Exception:
            return False


class SignatureAggregator:
    """
    Aggregates partial signatures into full ECDSA signatures.
    
    Uses additive homomorphism:
    z = sum(z_i) = sum(r_i + e * λ_i * x_i) = r + e * x
    
    Where:
    - r = sum(r_i) is the aggregated nonce
    - x = sum(λ_i * x_i) is the private key
    """
    
    CURVE_ORDER = SECP256k1.order
    G = SECP256k1.generator
    
    def aggregate_signatures(
        self,
        partial_sigs: List[PartialSignature],
        context: SigningContext
    ) -> ECDSASignature:
        """
        Aggregate partial signatures into full signature.
        
        Args:
            partial_sigs: List of partial signatures from participating parties
            context: Signing context with aggregated nonce and challenge
            
        Returns:
            Full ECDSASignature (r, s)
        """
        if len(partial_sigs) < 2:
            raise ValueError("Need at least 2 partial signatures")
        
        # Aggregate signature shares: z = sum(z_i)
        z = 0
        indices = [sig.index for sig in partial_sigs]
        
        for sig in partial_sigs:
            if sig.z is None:
                raise ValueError(f"Partial signature from party {sig.party_id} missing z")
            
            # Apply Lagrange coefficient for this party
            lagrange_coeff = DistributedKeyGenerator.lagrange_coefficient(
                sig.index,
                indices,
                self.CURVE_ORDER
            )
            
            # Add weighted share
            weighted_share = (sig.z * lagrange_coeff) % self.CURVE_ORDER
            z = (z + weighted_share) % self.CURVE_ORDER
        
        # r is x-coordinate of aggregated nonce R
        r = context.aggregated_nonce.x() % self.CURVE_ORDER
        
        # s = z (the aggregated signature share)
        s = z
        
        # Ensure low-s value (BIP-62)
        if s > self.CURVE_ORDER // 2:
            s = self.CURVE_ORDER - s
        
        return ECDSASignature(r, s)
    
    def aggregate_simple(
        self,
        partial_sigs: List[PartialSignature],
        aggregated_nonce: Point
    ) -> ECDSASignature:
        """
        Simplified aggregation without Lagrange interpolation.
        
        Used when all parties are trusted and present.
        """
        if len(partial_sigs) < 2:
            raise ValueError("Need at least 2 partial signatures")
        
        # Simple sum of shares
        z = sum(sig.z for sig in partial_sigs if sig.z is not None) % self.CURVE_ORDER
        
        r = aggregated_nonce.x() % self.CURVE_ORDER
        s = z
        
        if s > self.CURVE_ORDER // 2:
            s = self.CURVE_ORDER - s
        
        return ECDSASignature(r, s)
    
    def verify_aggregated_signature(
        self,
        sig: ECDSASignature,
        message_hash: bytes,
        public_key: bytes
    ) -> bool:
        """
        Verify an aggregated signature.
        
        This is standard ECDSA verification.
        """
        return sig.verify(message_hash, public_key)
    
    def create_precommitment_hash(
        self,
        party_id: int,
        public_nonce: Point,
        message_hash: bytes
    ) -> bytes:
        """
        Create hash for pre-commitment in interactive signing.
        
        Used in 3-round protocols to prevent malicious nonce aggregation.
        """
        h = hashlib.sha256()
        h.update(b"precommit")
        h.update(party_id.to_bytes(4, 'big'))
        
        if hasattr(public_nonce, 'to_bytes'):
            h.update(public_nonce.to_bytes())
        else:
            h.update(str(public_nonce).encode())
        
        h.update(message_hash)
        
        return h.digest()


class ThresholdSignatureScheme:
    """
    High-level interface for threshold signing.
    
    Combines DKG, signing, and aggregation into single interface.
    """
    
    def __init__(self, n: int, threshold: int):
        self.n = n
        self.threshold = threshold
        self.dkg = DistributedKeyGenerator(n, threshold)
    
    def generate_keys(self) -> List:
        """Generate key shares for all parties"""
        return self.dkg.generate_key_shares()
    
    def create_signer(self, key_share) -> 'ThresholdSigner':
        """Create signer for a party"""
        from .signer import ThresholdSigner
        return ThresholdSigner(key_share, key_share.party_id)
    
    def aggregate(
        self,
        partial_sigs: List[PartialSignature],
        context: SigningContext
    ) -> ECDSASignature:
        """Aggregate signatures"""
        aggregator = SignatureAggregator()
        return aggregator.aggregate_signatures(partial_sigs, context)
    
    def verify(
        self,
        sig: ECDSASignature,
        message_hash: bytes,
        public_key: bytes
    ) -> bool:
        """Verify signature"""
        return sig.verify(message_hash, public_key)


def test_threshold_signature():
    """Test the complete threshold signing flow"""
    import os
    os.environ['CRYPTOGRAPHY_OPENSSL_NO_LEGACY'] = '1'
    
    print("=== Testing Threshold Signature Scheme ===\n")
    
    # Setup: 5 parties, threshold 3
    n, t = 5, 3
    scheme = ThresholdSignatureScheme(n, t)
    
    # Generate keys
    print("1. Generating key shares...")
    shares = scheme.generate_keys()
    print(f"   Generated {len(shares)} shares")
    
    # Verify shares
    print("\n2. Verifying shares...")
    for share in shares:
        valid = scheme.dkg.verify_share(share)
        print(f"   Party {share.party_id}: {'✓' if valid else '✗'}")
    
    # Create signers
    print("\n3. Creating signers...")
    signers = [scheme.create_signer(share) for share in shares]
    
    # Message to sign
    message = b"Hello, Threshold World!"
    message_hash = hashlib.sha256(message).digest()
    print(f"\n4. Message to sign: {message.decode()}")
    print(f"   Hash: {message_hash.hex()[:32]}...")
    
    # Phase 1: Generate commitments
    print("\n5. Phase 1: Generating commitments...")
    partial_sigs = []
    for signer in signers[:t]:  # Use first t signers
        sig = signer.create_partial_signature(message_hash)
        partial_sigs.append(sig)
        print(f"   Party {signer.party_id}: commitment generated")
    
    # Create signing session and aggregate commitments
    from .signer import SigningSession
    session = SigningSession(message_hash, t)
    
    for sig in partial_sigs:
        session.add_commitment(sig)
    
    context = session.finalize_commitments()
    print(f"   Aggregated nonce computed")
    print(f"   Challenge: {hex(context.challenge)[:20]}...")
    
    # Phase 2: Create signature shares
    print("\n6. Phase 2: Creating signature shares...")
    completed_sigs = []
    for signer in signers[:t]:
        sig = signer.create_partial_signature(message_hash, context)
        completed_sigs.append(sig)
        print(f"   Party {signer.party_id}: signature share created")
    
    # Aggregate signatures
    print("\n7. Aggregating signatures...")
    full_sig = scheme.aggregate(completed_sigs, context)
    print(f"   Signature r: {hex(full_sig.r)[:20]}...")
    print(f"   Signature s: {hex(full_sig.s)[:20]}...")
    
    # Verify
    print("\n8. Verifying signature...")
    public_key = shares[0].public_key
    valid = scheme.verify(full_sig, message_hash, public_key)
    print(f"   Signature valid: {'✓' if valid else '✗'}")
    
    print("\n=== Test Complete ===")
    return valid


if __name__ == "__main__":
    test_threshold_signature()
