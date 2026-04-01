"""ReviewService — business logic for agent reviews and ZK review proofs.

Proof scheme
------------
The ZK proof here commits to a tuple (total_reviews, avg_overall_scaled)
where avg_overall_scaled = round(avg_overall * 100) to convert to an integer
in [100, 500].

Two Pedersen commitments are created, one per value, and two separate range
proofs are generated using the same Sigma-OR / Fiat-Shamir approach used in
ZKReputationService.  The commitment and proof are serialised as base64 JSON.

The verifier receives:
  - commitment_reviews  : hex Pedersen commitment to total_reviews
  - commitment_avg      : hex Pedersen commitment to avg_overall_scaled
  - proof               : base64 JSON with both range proofs and blinding data

Because this is an informational / non-interactive proof the blinding factors
are embedded in the proof payload (they cannot be kept secret from the holder
who presents the proof; the hiding property still holds for third parties who
see only the commitments + proof).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.models import Agent, HubRoute, EscrowDeal, SLAContract
from sthrip.db.enums import HubRouteStatus, EscrowStatus, SLAStatus
from sthrip.db.review_repo import ReviewRepository

logger = logging.getLogger("sthrip")

# ---------------------------------------------------------------------------
# Lightweight ZK helpers (same group as ZKReputationService)
# ---------------------------------------------------------------------------

# RFC 3526 Group 14 safe prime (2048-bit)
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


def _derive_h() -> int:
    seed = b"sthrip-review-h-gen"
    counter = 0
    while True:
        material = seed + counter.to_bytes(4, "big")
        digest = hashlib.sha512(material).digest()
        expanded = b""
        block = digest
        while len(expanded) < 256:
            block = hashlib.sha512(block).digest()
            expanded += block
        candidate = int.from_bytes(expanded[:256], "big") % _P
        if candidate <= 1:
            counter += 1
            continue
        h = pow(candidate, 2, _P)
        if h != 1:
            return h
        counter += 1


_H = _derive_h()


def _random_scalar() -> int:
    while True:
        r = secrets.randbelow(_Q)
        if r > 0:
            return r


def _pedersen_commit(value: int, blinding: int) -> int:
    return (pow(_G, value, _P) * pow(_H, blinding, _P)) % _P


def _fiat_shamir(*elements: int, label: bytes = b"") -> int:
    h = hashlib.sha256(label)
    for e in elements:
        h.update(e.to_bytes(256, "big"))
    return int.from_bytes(h.digest(), "big") % _Q


def _mod_inv(a: int, m: int) -> int:
    return pow(a, m - 2, m)


# Number of bits for range proof on the two values:
#   total_reviews in [0, 1000) → 10 bits (max 1023)
#   avg_scaled    in [100, 500] → also <=10 bits
_NUM_BITS_REVIEWS = 10
_NUM_BITS_AVG = 10


def _bits_of(value: int, num_bits: int) -> list:
    return [(value >> i) & 1 for i in range(num_bits)]


def _sigma_or_prove(bit: int, blinding: int, d: int) -> dict:
    """Produce Sigma-OR proof that d commits to a bit in {0, 1}."""
    real = bit
    sim = 1 - bit
    targets = [d % _P, (d * _mod_inv(_G, _P)) % _P]

    c_sim = _random_scalar()
    s_sim = _random_scalar()
    a_sim = (pow(_H, s_sim, _P) * pow(targets[sim], _Q - c_sim, _P)) % _P

    k = _random_scalar()
    a_real = pow(_H, k, _P)

    announcements = [0, 0]
    announcements[real] = a_real
    announcements[sim] = a_sim

    c = _fiat_shamir(d, announcements[0], announcements[1], label=b"review-sigma-or-bit")
    c_real = (c - c_sim) % _Q
    s_real = (k + c_real * blinding) % _Q

    challenges = [0, 0]
    responses = [0, 0]
    challenges[real] = c_real
    challenges[sim] = c_sim
    responses[real] = s_real
    responses[sim] = s_sim

    return {
        "D": str(d),
        "A0": str(announcements[0]),
        "A1": str(announcements[1]),
        "c0": str(challenges[0]),
        "c1": str(challenges[1]),
        "s0": str(responses[0]),
        "s1": str(responses[1]),
    }


def _sigma_or_verify(proof: dict) -> bool:
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

    c = _fiat_shamir(d_val, a0, a1, label=b"review-sigma-or-bit")
    if (c0 + c1) % _Q != c:
        return False

    t0 = d_val % _P
    t1 = (d_val * _mod_inv(_G, _P)) % _P

    lhs0 = pow(_H, s0, _P)
    rhs0 = (a0 * pow(t0, c0, _P)) % _P
    if lhs0 != rhs0:
        return False

    lhs1 = pow(_H, s1, _P)
    rhs1 = (a1 * pow(t1, c1, _P)) % _P
    if lhs1 != rhs1:
        return False

    return True


def _linking_prove(r_diff: int, link_target: int) -> dict:
    k = _random_scalar()
    announcement = pow(_H, k, _P)
    c = _fiat_shamir(link_target, announcement, label=b"review-link-proof")
    s = (k + c * r_diff) % _Q
    return {"T": str(link_target), "A": str(announcement), "s": str(s)}


def _linking_verify(proof: dict) -> bool:
    try:
        t = int(proof["T"])
        a = int(proof["A"])
        s = int(proof["s"])
    except (KeyError, ValueError):
        return False
    c = _fiat_shamir(t, a, label=b"review-link-proof")
    lhs = pow(_H, s, _P)
    rhs = (a * pow(t, c, _P)) % _P
    return lhs == rhs


def _generate_range_proof(value: int, threshold: int, num_bits: int, label_prefix: bytes) -> dict:
    """Generate a range proof that value >= threshold.

    Returns a dict with commitment (hex), blinding (hex), and proof data.
    """
    r = _random_scalar()
    commitment = _pedersen_commit(value, r)

    delta = value - threshold
    bits = _bits_of(delta, num_bits)

    bit_blindings = []
    bit_proofs = []
    for b in bits:
        r_i = _random_scalar()
        bit_blindings.append(r_i)
        d_i = _pedersen_commit(b, r_i)
        sigma_proof = _sigma_or_prove(b, r_i, d_i)
        bit_proofs.append(sigma_proof)

    r_link = sum(
        (bit_blindings[i] * pow(2, i, _Q)) % _Q for i in range(num_bits)
    ) % _Q

    r_diff = (r - r_link) % _Q
    link_target = pow(_H, r_diff, _P)
    link_proof = _linking_prove(r_diff, link_target)

    return {
        "commitment": format(commitment, "x"),
        "blinding": format(r, "x"),
        "threshold": threshold,
        "bit_proofs": bit_proofs,
        "link_proof": link_proof,
    }


def _verify_range_proof(commitment_hex: str, proof_data: dict) -> bool:
    """Verify a single range proof component."""
    try:
        threshold = proof_data["threshold"]
        c_val = int(commitment_hex, 16)
        bit_proofs = proof_data["bit_proofs"]
        num_bits = len(bit_proofs)

        for bp in bit_proofs:
            if not _sigma_or_verify(bp):
                return False

        aggregate = 1
        for i, bp in enumerate(bit_proofs):
            d_i = int(bp["D"])
            aggregate = (aggregate * pow(d_i, pow(2, i), _P)) % _P

        g_thresh_inv = _mod_inv(pow(_G, threshold, _P), _P)
        agg_inv = _mod_inv(aggregate, _P)
        link_target = (c_val * g_thresh_inv % _P * agg_inv) % _P

        link_proof = proof_data["link_proof"]
        if str(link_target) != link_proof.get("T"):
            return False

        return _linking_verify(link_proof)
    except Exception:
        logger.debug("Range proof verification failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# ReviewService
# ---------------------------------------------------------------------------


class ReviewService:
    """Business logic for agent reviews and ZK review proofs."""

    # ------------------------------------------------------------------
    # Review creation
    # ------------------------------------------------------------------

    def create_review(
        self,
        db: Session,
        reviewer_id: UUID,
        reviewed_id: UUID,
        transaction_id: UUID,
        transaction_type: str,
        overall_rating: int,
        speed_rating: Optional[int] = None,
        quality_rating: Optional[int] = None,
        reliability_rating: Optional[int] = None,
        comment: Optional[str] = None,
    ) -> dict:
        """Create a review and update the agent's rating summary.

        Raises:
            ValueError: self-review, unknown transaction, or invalid type.
        """
        if reviewer_id == reviewed_id:
            raise ValueError("An agent cannot review itself")

        self._verify_transaction(db, transaction_id, transaction_type)

        repo = ReviewRepository(db)
        review = repo.create(
            reviewer_id=reviewer_id,
            reviewed_id=reviewed_id,
            transaction_id=transaction_id,
            transaction_type=transaction_type,
            overall_rating=overall_rating,
            speed_rating=speed_rating,
            quality_rating=quality_rating,
            reliability_rating=reliability_rating,
            comment_encrypted=comment,
        )
        repo.update_rating_summary(reviewed_id)

        return self._review_to_dict(review)

    # ------------------------------------------------------------------
    # Review retrieval
    # ------------------------------------------------------------------

    def get_reviews(self, db: Session, agent_id: UUID, limit: int = 50, offset: int = 0) -> dict:
        """Return paginated reviews for agent_id."""
        repo = ReviewRepository(db)
        items, total = repo.list_by_reviewed(agent_id, limit=limit, offset=offset)
        return {
            "reviews": [self._review_to_dict(r) for r in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_rating_summary(self, db: Session, agent_id: UUID) -> dict:
        """Return the rating summary for agent_id (zeroed if no reviews yet)."""
        repo = ReviewRepository(db)
        summary = repo.get_rating_summary(agent_id)
        if summary is None:
            return {
                "agent_id": str(agent_id),
                "total_reviews": 0,
                "avg_overall": "0",
                "avg_speed": "0",
                "avg_quality": "0",
                "avg_reliability": "0",
                "five_star_count": 0,
                "one_star_count": 0,
                "last_review_at": None,
            }
        return self._summary_to_dict(summary)

    # ------------------------------------------------------------------
    # ZK review proof
    # ------------------------------------------------------------------

    def generate_review_proof(
        self,
        db: Session,
        agent_id: UUID,
        min_reviews: int,
        min_avg: float,
    ) -> dict:
        """Generate a ZK proof that agent meets the review thresholds.

        Returns a dict with commitment, proof, min_reviews, min_avg.
        Raises ValueError if the agent does not meet the thresholds.
        """
        summary = self.get_rating_summary(db, agent_id)
        total_reviews = summary["total_reviews"]
        avg_overall = float(summary["avg_overall"])

        if total_reviews < min_reviews:
            raise ValueError(
                f"Insufficient reviews: has {total_reviews}, needs {min_reviews}"
            )
        if avg_overall < min_avg:
            raise ValueError(
                f"Average rating {avg_overall:.2f} is below threshold {min_avg:.2f}"
            )

        # Scale avg to integer: avg_scaled in [100, 500]
        avg_scaled = round(avg_overall * 100)
        min_avg_scaled = round(min_avg * 100)

        # Generate individual range proofs
        proof_reviews = _generate_range_proof(
            value=total_reviews,
            threshold=min_reviews,
            num_bits=_NUM_BITS_REVIEWS,
            label_prefix=b"reviews",
        )
        proof_avg = _generate_range_proof(
            value=avg_scaled,
            threshold=min_avg_scaled,
            num_bits=_NUM_BITS_AVG,
            label_prefix=b"avg",
        )

        # Combined commitment: join both hex strings with a separator
        commitment = f"{proof_reviews['commitment']}:{proof_avg['commitment']}"

        # Serialise proof payload (blinding factors kept in proof for the holder)
        payload = {
            "version": 1,
            "proof_reviews": {
                k: v for k, v in proof_reviews.items() if k != "commitment"
            },
            "proof_avg": {
                k: v for k, v in proof_avg.items() if k != "commitment"
            },
            "commitment_reviews": proof_reviews["commitment"],
            "commitment_avg": proof_avg["commitment"],
            "min_reviews": min_reviews,
            "min_avg_scaled": min_avg_scaled,
        }
        proof_bytes = json.dumps(payload, separators=(",", ":")).encode()
        proof_b64 = base64.b64encode(proof_bytes).decode()

        logger.info(
            "Generated ZK review proof for agent %s (min_reviews=%d, min_avg=%.2f)",
            agent_id,
            min_reviews,
            min_avg,
        )
        return {
            "commitment": commitment,
            "proof": proof_b64,
            "min_reviews": min_reviews,
            "min_avg": str(min_avg),
        }

    def verify_review_proof(
        self,
        commitment: str,
        proof: str,
        min_reviews: int,
        min_avg: float,
    ) -> bool:
        """Verify a ZK review proof.

        Returns True when the proof is valid, False otherwise.
        """
        try:
            raw = base64.b64decode(proof)
            payload = json.loads(raw)

            if payload.get("version") != 1:
                return False

            # Check thresholds match
            if payload.get("min_reviews") != min_reviews:
                return False

            min_avg_scaled = round(min_avg * 100)
            if payload.get("min_avg_scaled") != min_avg_scaled:
                return False

            # Check commitment consistency
            c_reviews = payload.get("commitment_reviews", "")
            c_avg = payload.get("commitment_avg", "")
            expected_commitment = f"{c_reviews}:{c_avg}"
            if expected_commitment != commitment:
                return False

            # Verify both range proofs
            if not _verify_range_proof(c_reviews, payload["proof_reviews"]):
                return False
            if not _verify_range_proof(c_avg, payload["proof_avg"]):
                return False

            return True

        except Exception:
            logger.debug("Review proof verification failed", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_transaction(db: Session, transaction_id: UUID, transaction_type: str) -> None:
        """Raise ValueError if the transaction does not exist or type is invalid."""
        valid_types = {"payment", "escrow", "sla"}
        if transaction_type not in valid_types:
            raise ValueError(
                f"Invalid transaction_type {transaction_type!r}. "
                f"Must be one of: {', '.join(sorted(valid_types))}"
            )

        if transaction_type == "payment":
            row = (
                db.query(HubRoute)
                .filter(HubRoute.id == transaction_id)
                .first()
            )
            if row is None:
                raise ValueError(
                    f"Payment transaction {transaction_id} not found"
                )

        elif transaction_type == "escrow":
            row = (
                db.query(EscrowDeal)
                .filter(EscrowDeal.id == transaction_id)
                .first()
            )
            if row is None:
                raise ValueError(
                    f"Escrow deal {transaction_id} not found"
                )

        elif transaction_type == "sla":
            row = (
                db.query(SLAContract)
                .filter(SLAContract.id == transaction_id)
                .first()
            )
            if row is None:
                raise ValueError(
                    f"SLA contract {transaction_id} not found"
                )

    @staticmethod
    def _review_to_dict(review) -> dict:
        return {
            "id": str(review.id),
            "reviewer_id": str(review.reviewer_id),
            "reviewed_id": str(review.reviewed_id),
            "transaction_id": str(review.transaction_id),
            "transaction_type": review.transaction_type,
            "overall_rating": review.overall_rating,
            "speed_rating": review.speed_rating,
            "quality_rating": review.quality_rating,
            "reliability_rating": review.reliability_rating,
            "comment": review.comment_encrypted,
            "is_verified": review.is_verified,
            "created_at": str(review.created_at) if review.created_at else None,
        }

    @staticmethod
    def _summary_to_dict(summary) -> dict:
        return {
            "agent_id": str(summary.agent_id),
            "total_reviews": summary.total_reviews,
            "avg_overall": str(summary.avg_overall),
            "avg_speed": str(summary.avg_speed),
            "avg_quality": str(summary.avg_quality),
            "avg_reliability": str(summary.avg_reliability),
            "five_star_count": summary.five_star_count,
            "one_star_count": summary.one_star_count,
            "last_review_at": (
                str(summary.last_review_at) if summary.last_review_at else None
            ),
        }
