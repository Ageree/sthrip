"""Phase 2 Sprint 2 — commission fee calculator.

Computes the per-transfer commission deducted from the *sender* at write time.
All math is done in **piconero** (1 XMR = 10**12 piconero) using integer
arithmetic to avoid float drift on dust transfers.

Pricing (locked in lead-decisions.md / product-spec.md AD-3):

* Free tier         → 30 bps (0.3%)
* Pro / Verified /
  Premium /
  Enterprise tiers  → 10 bps (0.1%)

Floor: ``MIN_FEE_PICONERO`` (1 piconero / atomic unit). Even dust and zero-
amount transfers pay the floor — keeps revenue counting trivially correct
and avoids "free transfer" loopholes.

The existing ``fee_collector.py`` (1% flat hub-routing fee) is kept around
because its FeeCollection rows live alongside ours; this module is *additive*
(commission stacks on the legacy hub fee — they record into the same
``fee_collections`` table with different ``source_type`` markers).
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Final

from sthrip.db.enums import AgentTier

# Basis-point constants. 10_000 bps == 100%.
FREE_TIER_RATE_BPS: Final[int] = 30   # 0.30 %
PRO_TIER_RATE_BPS: Final[int] = 10    # 0.10 %
BPS_DENOMINATOR: Final[int] = 10_000
MIN_FEE_PICONERO: Final[int] = 1


def rate_bps_for_tier(tier: AgentTier) -> int:
    """Return the basis-point rate to apply for ``tier``.

    Existing AgentTier enum has FREE / VERIFIED / PREMIUM / ENTERPRISE.
    Sprint 2 product spec calls the paid bucket "Pro+". All non-FREE tiers
    are mapped to the Pro rate. New tier values automatically get the Pro
    rate; only ``FREE`` is "Free".
    """
    if tier == AgentTier.FREE:
        return FREE_TIER_RATE_BPS
    return PRO_TIER_RATE_BPS


def compute_fee(amount: int, agent_tier: AgentTier) -> int:
    """Return the commission fee in piconero for ``amount`` piconero.

    ``amount`` and the return value are integer piconero. Math uses
    ``Decimal`` with ROUND_HALF_UP semantics so 100_000_000 * 30 / 10_000
    = 300_000 exactly — no float drift, deterministic on every platform.

    The floor ``MIN_FEE_PICONERO`` always applies, including for amount=0.
    """
    if not isinstance(amount, int):
        raise TypeError(
            f"compute_fee expects int piconero, got {type(amount).__name__}"
        )
    if amount < 0:
        raise ValueError(f"amount must be non-negative, got {amount}")

    rate_bps = rate_bps_for_tier(agent_tier)

    # Decimal math to keep half-up rounding deterministic and avoid float.
    raw = (Decimal(amount) * Decimal(rate_bps) / Decimal(BPS_DENOMINATOR)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    fee = int(raw)
    if fee < MIN_FEE_PICONERO:
        fee = MIN_FEE_PICONERO
    return fee


__all__ = [
    "FREE_TIER_RATE_BPS",
    "PRO_TIER_RATE_BPS",
    "BPS_DENOMINATOR",
    "MIN_FEE_PICONERO",
    "compute_fee",
    "rate_bps_for_tier",
]
