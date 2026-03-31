"""Hashcash-style proof-of-work for Sybil prevention at registration.

Clients must solve a computational puzzle before registering an agent.
This makes mass Sybil registration expensive without requiring identity
verification or external dependencies.

The service uses SHA-256 hashing with a configurable difficulty (number
of leading zero bits required).  A difficulty of 20 takes ~1 second on
modern hardware; 24 takes ~16 seconds.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional


class POWService:
    """Issue, solve, and verify hashcash-style proof-of-work challenges."""

    def __init__(self, difficulty_bits: int = 20) -> None:
        if difficulty_bits < 1 or difficulty_bits > 32:
            raise ValueError("difficulty_bits must be between 1 and 32")
        self._difficulty_bits = difficulty_bits

    @property
    def difficulty_bits(self) -> int:
        return self._difficulty_bits

    def create_challenge(self, ttl_minutes: int = 10) -> Dict[str, str]:
        """Create a new PoW challenge.

        Returns a dict with algorithm, difficulty_bits, nonce, and
        expires_at (ISO-8601 UTC).  The nonce is a 32-hex-char random
        token that binds the challenge to a single registration attempt.
        """
        if ttl_minutes < 1:
            raise ValueError("ttl_minutes must be >= 1")

        nonce = secrets.token_hex(16)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        return {
            "algorithm": "sha256",
            "difficulty_bits": self._difficulty_bits,
            "nonce": nonce,
            "expires_at": expires_at.isoformat(),
        }

    # ------------------------------------------------------------------
    # Solving (used by SDK auto-register and tests)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_hash(nonce: str, counter: int, difficulty_bits: int) -> bool:
        """Return True if sha256(nonce:counter) has the required leading zeros."""
        candidate = "{}:{}".format(nonce, counter)
        digest = hashlib.sha256(candidate.encode()).hexdigest()
        bits = bin(int(digest, 16))[2:].zfill(256)
        return bits[:difficulty_bits] == "0" * difficulty_bits

    def solve(self, challenge: Dict[str, object]) -> str:
        """Brute-force solve a challenge by incrementing a counter.

        Returns the counter value (as a string) that produces a hash
        with the required number of leading zero bits.

        NOTE: This is intentionally expensive -- use low difficulty in
        tests.
        """
        nonce = str(challenge["nonce"])
        target_bits = int(challenge["difficulty_bits"])
        counter = 0
        while True:
            if self._check_hash(nonce, counter, target_bits):
                return str(counter)
            counter += 1

    # ------------------------------------------------------------------
    # Verification (used by the registration endpoint)
    # ------------------------------------------------------------------

    def verify(self, challenge: Dict[str, object], solution: str) -> bool:
        """Verify a PoW solution against its challenge.

        Returns False if:
        - the challenge has expired
        - the solution does not produce a hash with enough leading zeros
        """
        # -- Expiry check --
        raw_expires = challenge.get("expires_at")
        if raw_expires is None:
            return False

        try:
            expires_at = datetime.fromisoformat(str(raw_expires))
        except (ValueError, TypeError):
            return False

        # Ensure timezone-aware comparison
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if datetime.now(timezone.utc) > expires_at:
            return False

        # -- Hash check --
        nonce = str(challenge.get("nonce", ""))
        try:
            difficulty_bits = int(challenge["difficulty_bits"])
        except (KeyError, ValueError, TypeError):
            return False

        try:
            counter = int(solution)
        except (ValueError, TypeError):
            return False

        return self._check_hash(nonce, counter, difficulty_bits)


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------

_default_service: Optional[POWService] = None


def get_pow_service(difficulty_bits: int = 20) -> POWService:
    """Return the module-level POWService singleton.

    The singleton is lazily created on first call.  Pass
    ``difficulty_bits`` to override the default difficulty (only
    takes effect on the first call).
    """
    global _default_service
    if _default_service is None:
        _default_service = POWService(difficulty_bits=difficulty_bits)
    return _default_service


def reset_pow_service() -> None:
    """Reset the singleton -- useful in tests."""
    global _default_service
    _default_service = None
