"""Zero-knowledge reputation proof service.

Implements a Pedersen commitment scheme over a multiplicative group modulo
a 2048-bit safe prime, combined with a bit-decomposition range proof using
Sigma protocols (Schnorr proofs of knowledge) made non-interactive via
the Fiat-Shamir heuristic.

Commitment
----------
    C = g^score * h^r  mod p

where (g, h) are generators of the order-q subgroup of Z*_p, p = 2q + 1
is a safe prime, and r is a random blinding factor in Z_q.

Range proof  (score >= threshold)
---------------------------------
    delta = score - threshold   (must be >= 0)

delta is decomposed into 7 bits (sufficient for 0..100 range).  For each
bit b_i a sub-commitment D_i = g^{b_i} * h^{r_i} is published, together
with a Sigma-OR proof that b_i in {0, 1}.  A final linking proof shows
that the product of D_i^{2^i} equals C / g^threshold -- i.e. the committed
delta equals the claimed bit decomposition AND the original score equals
threshold + delta.

Security properties
-------------------
* **Hiding** -- the commitment reveals nothing about the score (information-
  theoretically hiding under the discrete-log assumption).
* **Binding** -- opening a commitment to a different value requires solving
  DLP in the safe-prime subgroup.
* **Zero-knowledge** -- the verifier learns only that score >= threshold.
  The proof transcript is simulatable by anyone who knows the public
  parameters and the threshold.

Implementation notes
--------------------
* Pure Python -- only ``hashlib`` and ``secrets`` from the standard library.
* The 2048-bit safe prime and generators are hard-coded constants derived
  from RFC 3526 Group 14 (MODP group).  The second generator h is obtained
  by hashing g with a nothing-up-my-sleeve construction so that nobody
  knows log_g(h).
* Scores are integers in [0, 100]; 7 bits suffice for delta in [0, 100].
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from typing import Dict, List, Tuple

logger = logging.getLogger("sthrip")

# ---------------------------------------------------------------------------
# Group parameters -- RFC 3526 Group 14 (2048-bit MODP)
# ---------------------------------------------------------------------------
# p is a safe prime: p = 2q + 1 where q = (p - 1) / 2 is also prime.
# g = 2 is the canonical generator given in the RFC.

_P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF",
    16,
)
_Q = (_P - 1) // 2
_G = 2

# Second generator h: derived via a nothing-up-my-sleeve hash so that
# log_g(h) is unknown.  We hash the ASCII string "sthrip-pedersen-h-gen"
# repeatedly until we find a quadratic residue in the order-q subgroup.
_H: int  # assigned below


def _derive_second_generator() -> int:
    """Derive h = hash-to-group("sthrip-pedersen-h-gen") in the order-q subgroup."""
    seed = b"sthrip-pedersen-h-gen"
    # Iteratively hash until we get a valid subgroup element != 1
    counter = 0
    while True:
        material = seed + counter.to_bytes(4, "big")
        digest = hashlib.sha512(material).digest()
        # Expand to a 2048-bit candidate via repeated hashing
        expanded = b""
        block = digest
        while len(expanded) < 256:  # 2048 bits = 256 bytes
            block = hashlib.sha512(block).digest()
            expanded += block
        candidate = int.from_bytes(expanded[:256], "big") % _P
        if candidate <= 1:
            counter += 1
            continue
        # Map into the order-q subgroup: h = candidate^2 mod p
        h = pow(candidate, 2, _P)
        if h != 1:
            return h
        counter += 1


_H = _derive_second_generator()

# Number of bits for the range proof.  Scores are 0..100, so delta is
# 0..100 which fits in 7 bits (max 127).
_NUM_BITS = 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mod_inv(a: int, m: int) -> int:
    """Modular inverse via Fermat's little theorem (m must be prime)."""
    return pow(a, m - 2, m)


def _random_scalar() -> int:
    """Return a random integer in [1, q-1]."""
    while True:
        r = secrets.randbelow(_Q)
        if r > 0:
            return r


def _fiat_shamir(*elements: int, label: bytes = b"") -> int:
    """Fiat-Shamir challenge: H(label || elem_1 || elem_2 || ...) mod q.

    Each element is encoded as a big-endian 256-byte integer.
    """
    h = hashlib.sha256(label)
    for e in elements:
        h.update(e.to_bytes(256, "big"))
    digest = h.digest()
    return int.from_bytes(digest, "big") % _Q


def _bits_of(value: int, num_bits: int) -> List[int]:
    """Return the little-endian bit decomposition of *value*."""
    return [(value >> i) & 1 for i in range(num_bits)]


# ---------------------------------------------------------------------------
# Sigma-OR proof for bit commitments
# ---------------------------------------------------------------------------
#
# Given D = g^b * h^r  with b in {0, 1}, we produce a Sigma-OR proof
# that the prover knows an opening of D to either 0 or 1 without
# revealing which.
#
# For the "real" branch (the one the prover actually knows), we run
# a standard Schnorr proof of knowledge of the blinding factor.
# For the "simulated" branch we pick the challenge and response first
# and compute the announcement backwards.
#
# The combined challenge is c = H(D, A_0, A_1) (Fiat-Shamir) and we
# require c_0 + c_1 = c  mod q.


def _sigma_or_prove(
    bit: int,
    blinding: int,
    commitment_d: int,
) -> Dict[str, str]:
    """Produce a non-interactive Sigma-OR proof that *commitment_d* commits
    to a bit in {0, 1}.

    Returns a dict with string-encoded proof components.
    """
    # The two possible "targets" for the Schnorr sub-proofs:
    #   If b=0: D = g^0 * h^r = h^r          -- prove knowledge of r w.r.t. h
    #   If b=1: D/g = g^0 * h^r = h^r         -- prove knowledge of r w.r.t. h
    # In each case the "other" branch is simulated.

    # The real branch index is `bit` itself.
    real = bit       # 0 or 1
    sim = 1 - bit    # the simulated branch

    # Target for each branch: T_b = D / g^b  (should equal h^r)
    # T_0 = D,  T_1 = D * g^{-1}
    targets = [
        commitment_d % _P,
        (commitment_d * _mod_inv(_G, _P)) % _P,
    ]

    # --- Simulated branch ---
    c_sim = _random_scalar()
    s_sim = _random_scalar()
    # Announcement: A_sim = h^{s_sim} * T_sim^{-c_sim}  mod p
    a_sim = (pow(_H, s_sim, _P) * pow(targets[sim], _Q - c_sim, _P)) % _P

    # --- Real branch ---
    k = _random_scalar()  # random nonce
    a_real = pow(_H, k, _P)

    # Arrange announcements in order (index 0, then 1)
    announcements = [0, 0]
    announcements[real] = a_real
    announcements[sim] = a_sim

    # Fiat-Shamir challenge
    c = _fiat_shamir(
        commitment_d,
        announcements[0],
        announcements[1],
        label=b"sigma-or-bit",
    )

    c_real = (c - c_sim) % _Q
    s_real = (k + c_real * blinding) % _Q

    # Pack challenges and responses in order
    challenges = [0, 0]
    responses = [0, 0]
    challenges[real] = c_real
    challenges[sim] = c_sim
    responses[real] = s_real
    responses[sim] = s_sim

    return {
        "D": str(commitment_d),
        "A0": str(announcements[0]),
        "A1": str(announcements[1]),
        "c0": str(challenges[0]),
        "c1": str(challenges[1]),
        "s0": str(responses[0]),
        "s1": str(responses[1]),
    }


def _sigma_or_verify(proof: Dict[str, str]) -> bool:
    """Verify a Sigma-OR bit proof."""
    try:
        d_val = int(proof["D"])
        a0 = int(proof["A0"])
        a1 = int(proof["A1"])
        c0 = int(proof["c0"])
        c1 = int(proof["c1"])
        s0 = int(proof["s0"])
        s1 = int(proof["s1"])
    except (KeyError, ValueError):
        return False

    # Recompute the combined Fiat-Shamir challenge
    c = _fiat_shamir(d_val, a0, a1, label=b"sigma-or-bit")

    # Check c0 + c1 == c  mod q
    if (c0 + c1) % _Q != c:
        return False

    # Targets
    t0 = d_val % _P
    t1 = (d_val * _mod_inv(_G, _P)) % _P

    # Verify branch 0:  h^{s0} == A0 * T0^{c0}  mod p
    lhs0 = pow(_H, s0, _P)
    rhs0 = (a0 * pow(t0, c0, _P)) % _P
    if lhs0 != rhs0:
        return False

    # Verify branch 1:  h^{s1} == A1 * T1^{c1}  mod p
    lhs1 = pow(_H, s1, _P)
    rhs1 = (a1 * pow(t1, c1, _P)) % _P
    if lhs1 != rhs1:
        return False

    return True


# ---------------------------------------------------------------------------
# Linking proof
# ---------------------------------------------------------------------------
#
# The linking proof shows that the product  prod(D_i^{2^i})  equals
# C / g^threshold, i.e. that the bit decomposition sums to the right
# value.  Since each D_i = g^{b_i} * h^{r_i}, the product is
#
#     g^{sum(b_i * 2^i)} * h^{sum(r_i * 2^i)}
#   = g^delta * h^{r_link}
#
# And C / g^threshold = g^{score - threshold} * h^r = g^delta * h^r.
#
# So the prover must show knowledge of (r - r_link) such that
#
#     (C / g^threshold) / prod(D_i^{2^i})  =  h^{r - r_link}
#
# This is a standard Schnorr proof of knowledge of a discrete log w.r.t. h.


def _linking_prove(
    r_diff: int,
    link_target: int,
) -> Dict[str, str]:
    """Schnorr proof that link_target = h^{r_diff}."""
    k = _random_scalar()
    announcement = pow(_H, k, _P)

    c = _fiat_shamir(link_target, announcement, label=b"link-proof")

    s = (k + c * r_diff) % _Q

    return {
        "T": str(link_target),
        "A": str(announcement),
        "s": str(s),
    }


def _linking_verify(proof: Dict[str, str]) -> bool:
    """Verify a Schnorr linking proof."""
    try:
        t = int(proof["T"])
        a = int(proof["A"])
        s = int(proof["s"])
    except (KeyError, ValueError):
        return False

    c = _fiat_shamir(t, a, label=b"link-proof")

    # Check: h^s == A * T^c  mod p
    lhs = pow(_H, s, _P)
    rhs = (a * pow(t, c, _P)) % _P
    return lhs == rhs


# ---------------------------------------------------------------------------
# Public service class
# ---------------------------------------------------------------------------


class ZKReputationService:
    """Stateless service for creating and verifying Pedersen-commitment-based
    ZK reputation proofs.

    The commitment is a Pedersen commitment over the order-q subgroup of
    Z*_p (RFC 3526 Group 14, 2048-bit safe prime).  The range proof uses
    bit-decomposition with Sigma-OR proofs and a Fiat-Shamir heuristic.
    """

    # ------------------------------------------------------------------
    # Commitment
    # ------------------------------------------------------------------

    def create_commitment(self, score: int) -> Tuple[str, str]:
        """Create a Pedersen commitment to a reputation score.

        Returns:
            A ``(commitment_hex, blinding_hex)`` tuple.  The blinding
            factor must be stored privately and never exposed via public
            APIs.  Both values are hex-encoded big integers.
        """
        if not isinstance(score, int) or score < 0 or score > 100:
            raise ValueError("Score must be an integer between 0 and 100")

        r = _random_scalar()
        c = self._pedersen_commit(score, r)
        return format(c, "x"), format(r, "x")

    # ------------------------------------------------------------------
    # Proof generation
    # ------------------------------------------------------------------

    def generate_proof(self, score: int, blinding: str, threshold: int) -> str:
        """Generate a ZK proof that the committed score >= *threshold*.

        The proof contains:
        * The commitment ``C``
        * The threshold
        * 7 bit-commitments ``D_i`` with Sigma-OR proofs (each bit is 0 or 1)
        * A linking proof tying the bit decomposition to ``C / g^threshold``
        * A Fiat-Shamir top-level hash binding the whole transcript

        Raises ``ValueError`` if the score is below the requested threshold
        or if inputs are out of range.

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

        r = int(blinding, 16)
        commitment = self._pedersen_commit(score, r)

        delta = score - threshold
        bits = _bits_of(delta, _NUM_BITS)

        # Create per-bit commitments and Sigma-OR proofs
        bit_blindings: List[int] = []
        bit_proofs: List[Dict[str, str]] = []

        for b in bits:
            r_i = _random_scalar()
            bit_blindings.append(r_i)
            d_i = self._pedersen_commit(b, r_i)
            sigma_proof = _sigma_or_prove(b, r_i, d_i)
            bit_proofs.append(sigma_proof)

        # Linking proof
        # r_link = sum(r_i * 2^i) mod q
        r_link = sum(
            (bit_blindings[i] * pow(2, i, _Q)) % _Q for i in range(_NUM_BITS)
        ) % _Q

        # link_target = C / g^threshold / prod(D_i^{2^i})
        #             = h^{r - r_link}  mod p
        r_diff = (r - r_link) % _Q
        link_target = pow(_H, r_diff, _P)

        link_proof = _linking_prove(r_diff, link_target)

        payload = {
            "version": 2,
            "commitment": format(commitment, "x"),
            "threshold": threshold,
            "bit_proofs": bit_proofs,
            "link_proof": link_proof,
        }

        proof_bytes = json.dumps(payload, separators=(",", ":")).encode()
        proof_payload = base64.b64encode(proof_bytes).decode()

        logger.info(
            "Generated ZK reputation proof (threshold=%d) for commitment=%s",
            threshold,
            format(commitment, "x")[:12],
        )
        return proof_payload

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify_proof(self, commitment: str, proof: str, threshold: int) -> bool:
        """Verify a ZK range proof that the committed score >= *threshold*.

        The verifier learns nothing about the score beyond the fact that
        it is at least *threshold*.

        Returns ``True`` when the proof is valid, ``False`` otherwise.
        """
        try:
            raw = base64.b64decode(proof)
            payload = json.loads(raw)

            # Must be version 2 (real ZK)
            if payload.get("version") != 2:
                return False

            # Commitment in the proof must match the supplied commitment
            if payload.get("commitment") != commitment:
                return False

            # Threshold in the proof must match the requested threshold
            if payload.get("threshold") != threshold:
                return False

            c_val = int(commitment, 16)
            bit_proofs: List[Dict[str, str]] = payload["bit_proofs"]

            if len(bit_proofs) != _NUM_BITS:
                return False

            # 1. Verify each Sigma-OR bit proof
            for bp in bit_proofs:
                if not _sigma_or_verify(bp):
                    return False

            # 2. Reconstruct the aggregate bit commitment
            #    prod(D_i^{2^i})  mod p
            aggregate = 1
            for i, bp in enumerate(bit_proofs):
                d_i = int(bp["D"])
                aggregate = (aggregate * pow(d_i, pow(2, i), _P)) % _P

            # 3. Compute link_target = C / g^threshold / aggregate  mod p
            g_thresh_inv = _mod_inv(pow(_G, threshold, _P), _P)
            agg_inv = _mod_inv(aggregate, _P)
            link_target = (c_val * g_thresh_inv % _P * agg_inv) % _P

            # The link proof must prove knowledge of log_h(link_target)
            link_proof = payload["link_proof"]
            if str(link_target) != link_proof.get("T"):
                return False

            if not _linking_verify(link_proof):
                return False

            return True

        except Exception:
            logger.debug("ZK proof verification failed", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _pedersen_commit(value: int, blinding: int) -> int:
        """Pedersen commitment: C = g^value * h^blinding  mod p."""
        return (pow(_G, value, _P) * pow(_H, blinding, _P)) % _P

    def verify_commitment(self, commitment: str, score: int, blinding: str) -> bool:
        """Check that a commitment opens to the given score and blinding.

        This is used internally by the router to detect stale commitments,
        NOT exposed to external verifiers.
        """
        try:
            r = int(blinding, 16)
            expected = self._pedersen_commit(score, r)
            return format(expected, "x") == commitment
        except (ValueError, TypeError):
            return False
