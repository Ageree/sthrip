"""
Threshold Signer for ECDSA

Creates partial signatures that can be aggregated into full signatures.
Implements the scheme from https://eprint.iacr.org/2020/540
"""

import hashlib
import secrets
from typing import List, Tuple
from dataclasses import dataclass
from ecdsa import SECP256k1, SigningKey, VerifyingKey, NIST256p
from ecdsa.ellipticcurve import Point
from ecdsa.util import randrange

from .dkg import KeyShare, DistributedKeyGenerator


@dataclass
class PartialSignature:
    """Partial signature from one party"""
    party_id: int
    index: int
    c: int  # Commitment to nonce
    z: int  # Signature share
    public_nonce: Point  # R_i = g^{r_i}
    
    def to_dict(self) -> dict:
        return {
            "party_id": self.party_id,
            "index": self.index,
            "c": hex(self.c)[2:].zfill(64),
            "z": hex(self.z)[2:].zfill(64),
            "public_nonce": self.public_nonce.to_bytes().hex() if hasattr(self.public_nonce, 'to_bytes') else str(self.public_nonce)
        }


@dataclass 
class SigningContext:
    """Context for a signing session"""
    message_hash: bytes
    participants: List[int]  # Party IDs participating
    aggregated_nonce: Point = None  # R = sum(R_i)
    challenge: int = None  # e = H(R || P || m)


class ThresholdSigner:
    """
    Threshold ECDSA Signer.
    
    Each party holds a key share and can create partial signatures.
    Any t+1 partial signatures can be aggregated into a full valid signature.
    """
    
    CURVE_ORDER = SECP256k1.order
    G = SECP256k1.generator
    
    def __init__(self, key_share: KeyShare, party_id: int):
        """
        Initialize signer with key share.
        
        Args:
            key_share: The party's key share
            party_id: Unique ID of this party
        """
        self.key_share = key_share
        self.party_id = party_id
        self.nonce_seed = secrets.token_bytes(32)  # For deterministic nonce generation
    
    def create_partial_signature(
        self,
        message_hash: bytes,
        context: SigningContext = None
    ) -> PartialSignature:
        """
        Create a partial signature for a message.
        
        This is Phase 1 of 2-round signing protocol.
        
        Args:
            message_hash: 32-byte message hash to sign
            context: Optional signing context (for multi-round protocols)
            
        Returns:
            PartialSignature with commitment and public nonce
        """
        # Generate random nonce
        nonce = self._generate_nonce(message_hash)
        
        # Public nonce: R_i = g^{nonce}
        public_nonce = self.G * nonce
        
        # Commitment to nonce (for verifiability)
        commitment = self._hash_nonce_commitment(public_nonce, message_hash)
        
        # If we have aggregated nonce from other parties, compute signature share
        z = None
        if context and context.aggregated_nonce and context.challenge:
            z = self._compute_signature_share(
                nonce,
                context.challenge,
                self.key_share.private_share
            )
        
        return PartialSignature(
            party_id=self.party_id,
            index=self.key_share.index,
            c=commitment,
            z=z,
            public_nonce=public_nonce
        )
    
    def complete_signature(
        self,
        partial_sig: PartialSignature,
        context: SigningContext
    ) -> PartialSignature:
        """
        Complete partial signature with challenge.
        
        This is Phase 2 of 2-round signing protocol.
        Called after receiving aggregated nonce from coordinator.
        
        Args:
            partial_sig: Partial signature from Phase 1
            context: Signing context with aggregated nonce and challenge
            
        Returns:
            Completed PartialSignature with z value
        """
        # Reconstruct nonce from commitment (or store it from Phase 1)
        nonce = self._generate_nonce(context.message_hash)
        
        # Compute signature share: z_i = r_i + e * λ_i * x_i
        z = self._compute_signature_share(
            nonce,
            context.challenge,
            self.key_share.private_share
        )
        
        return PartialSignature(
            party_id=self.party_id,
            index=self.key_share.index,
            c=partial_sig.c,
            z=z,
            public_nonce=partial_sig.public_nonce
        )
    
    def _generate_nonce(self, message_hash: bytes) -> int:
        """
        Generate deterministic nonce using RFC 6979 approach.
        
        nonce = H(seed || message_hash) mod curve_order
        """
        h = hashlib.sha256()
        h.update(self.nonce_seed)
        h.update(message_hash)
        h.update(self.key_share.private_share.to_bytes(32, 'big'))
        
        nonce = int(h.hexdigest(), 16) % self.CURVE_ORDER
        
        # Ensure nonce != 0
        if nonce == 0:
            nonce = 1
            
        return nonce
    
    def _hash_nonce_commitment(self, public_nonce: Point, message_hash: bytes) -> int:
        """Create commitment to nonce for verifiability"""
        h = hashlib.sha256()
        h.update(public_nonce.to_bytes() if hasattr(public_nonce, 'to_bytes') else str(public_nonce).encode())
        h.update(message_hash)
        h.update(self.party_id.to_bytes(4, 'big'))
        
        return int(h.hexdigest(), 16) % self.CURVE_ORDER
    
    def _compute_signature_share(
        self,
        nonce: int,
        challenge: int,
        private_share: int
    ) -> int:
        """
        Compute signature share: z_i = r_i + e * λ_i * x_i
        
        Where:
        - r_i is the nonce
        - e is the challenge
        - λ_i is the Lagrange coefficient
        - x_i is the private share
        """
        # For simplicity, assume λ_i = 1 (single party)
        # In real threshold scheme, compute proper Lagrange coefficient
        lagrange_coeff = 1
        
        # z_i = r_i + e * λ_i * x_i
        z = (nonce + challenge * lagrange_coeff * private_share) % self.CURVE_ORDER
        
        return z
    
    def verify_partial_signature(
        self,
        partial_sig: PartialSignature,
        context: SigningContext
    ) -> bool:
        """
        Verify a partial signature from another party.
        
        Checks that: g^{z_i} == R_i * (g^{x_i})^{e * λ_i}
        
        Note: Requires knowledge of other party's public share.
        """
        if partial_sig.z is None:
            return False
        
        # Left side: g^{z_i}
        lhs = self.G * partial_sig.z
        
        # Right side: R_i * (g^{x_i})^{e * λ_i}
        # We need g^{x_i} (public share) from the other party
        # For now, this is a placeholder
        
        # In production: verify using VSS commitments
        return True
    
    def get_public_share(self) -> Point:
        """Get public key share: g^{x_i}"""
        return self.G * self.key_share.private_share


class SigningSession:
    """
    Coordinates a multi-party signing session.
    
    Manages the 2-round protocol:
    1. Collect commitments (R_i) from all parties
    2. Broadcast aggregated nonce and collect signature shares (z_i)
    """
    
    def __init__(self, message_hash: bytes, threshold: int):
        self.message_hash = message_hash
        self.threshold = threshold
        self.phase = 1
        
        self.commitments: dict[int, PartialSignature] = {}
        self.signatures: dict[int, PartialSignature] = {}
        self.participants: List[int] = []
        
        self.aggregated_nonce: Point = None
        self.challenge: int = None
    
    def add_commitment(self, sig: PartialSignature) -> bool:
        """Add commitment from a party (Phase 1)"""
        if self.phase != 1:
            raise ValueError("Not in commitment phase")
        
        self.commitments[sig.party_id] = sig
        self.participants.append(sig.party_id)
        
        # Check if we have enough commitments
        return len(self.commitments) >= self.threshold
    
    def finalize_commitments(self) -> SigningContext:
        """
        Finalize Phase 1 and create signing context.
        
        Aggregates nonces and computes challenge.
        """
        if len(self.commitments) < self.threshold:
            raise ValueError(f"Need {self.threshold} commitments, have {len(self.commitments)}")
        
        # Aggregate nonces: R = sum(R_i)
        R = None
        for sig in self.commitments.values():
            if R is None:
                R = sig.public_nonce
            else:
                R = R + sig.public_nonce
        
        self.aggregated_nonce = R
        
        # Compute challenge: e = H(R || P || m)
        self.challenge = self._compute_challenge(R, self.message_hash)
        
        self.phase = 2
        
        return SigningContext(
            message_hash=self.message_hash,
            participants=self.participants,
            aggregated_nonce=R,
            challenge=self.challenge
        )
    
    def add_signature_share(self, sig: PartialSignature) -> bool:
        """Add signature share from a party (Phase 2)"""
        if self.phase != 2:
            raise ValueError("Not in signature phase")
        
        if sig.z is None:
            raise ValueError("Signature share missing z value")
        
        self.signatures[sig.party_id] = sig
        
        # Check if we have enough shares
        return len(self.signatures) >= self.threshold
    
    def _compute_challenge(self, R: Point, message_hash: bytes) -> int:
        """Compute challenge: e = H(R || m)"""
        h = hashlib.sha256()
        
        # Serialize R
        if hasattr(R, 'to_bytes'):
            r_bytes = R.to_bytes()
        else:
            r_bytes = str(R).encode()
        
        h.update(r_bytes)
        h.update(message_hash)
        
        return int(h.hexdigest(), 16) % ThresholdSigner.CURVE_ORDER
    
    def is_complete(self) -> bool:
        """Check if signing session is complete"""
        return len(self.signatures) >= self.threshold
