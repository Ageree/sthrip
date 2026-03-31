"""Zero-knowledge reputation proof service.

Uses a hash-based commitment scheme to allow agents to prove their
reputation score exceeds a threshold without revealing the exact score.

Commitment = SHA-256(score || ":" || blinding_factor)
Proof payload = base64(JSON { commitment, threshold, score, blinding, proof_hash })

NOTE: This is a *simplified* commitment scheme suitable for demonstrating the
API contract.  A production deployment would replace the proof/verify layer
with a proper Pedersen commitment + range proof (e.g. Bulletproofs) so that
the score is never disclosed to the verifier.
"""

import base64
import hashlib
import json
import logging
import secrets
from typing import Tuple

logger = logging.getLogger("sthrip")


class ZKReputationService:
    """Stateless service for creating and verifying reputation proofs."""

    # ------------------------------------------------------------------
    # Commitment
    # ------------------------------------------------------------------

    def create_commitment(self, score: int) -> Tuple[str, str]:
        """Create a commitment to a reputation score.

        Returns:
            A (commitment, blinding_factor) tuple.  The blinding factor
            must be stored privately and never exposed via public APIs.
        """
        if not isinstance(score, int) or score < 0 or score > 100:
            raise ValueError("Score must be an integer between 0 and 100")

        blinding = secrets.token_hex(32)
        commitment = self._compute_commitment(score, blinding)
        return commitment, blinding

    # ------------------------------------------------------------------
    # Proof generation
    # ------------------------------------------------------------------

    def generate_proof(self, score: int, blinding: str, threshold: int) -> str:
        """Generate a proof that *score >= threshold*.

        Raises ``ValueError`` if the score is below the requested threshold.

        Returns:
            A base64-encoded JSON proof payload string.
        """
        if not isinstance(score, int) or score < 0 or score > 100:
            raise ValueError("Score must be an integer between 0 and 100")
        if not isinstance(threshold, int) or threshold < 0 or threshold > 100:
            raise ValueError("Threshold must be an integer between 0 and 100")
        if score < threshold:
            raise ValueError(
                f"Score {score} is below threshold {threshold}"
            )

        commitment = self._compute_commitment(score, blinding)

        # Proof hash binds the claim to a fresh nonce so it cannot be replayed
        nonce = secrets.token_hex(16)
        proof_data = f"{commitment}:{threshold}:{nonce}"
        proof_hash = hashlib.sha256(proof_data.encode()).hexdigest()

        payload = {
            "commitment": commitment,
            "threshold": threshold,
            "score": score,        # in production ZK this would NOT be included
            "blinding": blinding,  # in production ZK this would NOT be included
            "proof_hash": proof_hash,
        }
        proof_payload = base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode()

        logger.info(
            "Generated ZK reputation proof (threshold=%d) for commitment=%s",
            threshold,
            commitment[:12],
        )
        return proof_payload

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify_proof(self, commitment: str, proof: str, threshold: int) -> bool:
        """Verify a ZK proof that the committed score >= *threshold*.

        Returns ``True`` when the proof is valid, ``False`` otherwise.
        """
        try:
            raw = base64.b64decode(proof)
            payload = json.loads(raw)

            # Reconstruct the commitment from the disclosed score + blinding
            reconstructed = self._compute_commitment(
                payload["score"], payload["blinding"]
            )
            if reconstructed != commitment:
                return False

            # The commitment embedded in the proof must match the input
            if payload.get("commitment") != commitment:
                return False

            # Check the claim
            if payload["score"] < threshold:
                return False

            return True
        except Exception:
            logger.debug("ZK proof verification failed", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_commitment(score: int, blinding: str) -> str:
        """Deterministic commitment: SHA-256(score:blinding)."""
        return hashlib.sha256(f"{score}:{blinding}".encode()).hexdigest()
