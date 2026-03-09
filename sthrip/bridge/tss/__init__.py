"""
Threshold Signature Scheme (TSS) Implementation

Production-ready TSS using ECDSA threshold signatures.
Supports 3-of-5 threshold scheme for bridge operations.
"""

from .dkg import DistributedKeyGenerator, KeyShare
from .signer import ThresholdSigner, PartialSignature
from .aggregator import SignatureAggregator

__all__ = [
    "DistributedKeyGenerator",
    "KeyShare", 
    "ThresholdSigner",
    "PartialSignature",
    "SignatureAggregator",
]
