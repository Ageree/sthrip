"""
TreasuryService -- business logic for agent treasury management.

Handles policy CRUD, portfolio status, rebalancing, and history.
Delegates currency conversions to ConversionService and data access
to TreasuryRepository / BalanceRepository.
"""

import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.balance_repo import BalanceRepository
from sthrip.db.models import AgentBalance
from sthrip.db.treasury_repo import TreasuryRepository
from sthrip.services.conversion_service import ConversionService, FALLBACK_RATES

logger = logging.getLogger("sthrip")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Supported treasury tokens and their USD-denominated rate keys in FALLBACK_RATES
_SUPPORTED_TOKENS = ("XMR", "xUSD", "xEUR")

# How to convert a token balance to USD-equivalent for allocation calculations
_TOKEN_TO_USD_RATE_KEY: Dict[str, Optional[str]] = {
    "XMR": "XMR_USD",
    "xUSD": None,   # 1:1 with USD
    "xEUR": "XMR_EUR",  # special handling below
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _token_usd_value(token: str, amount: Decimal) -> Decimal:
    """Convert a token amount to its USD-equivalent value using fallback rates."""
    if amount == Decimal("0"):
        return Decimal("0")
    if token == "xUSD":
        return amount
    if token == "XMR":
        rate = FALLBACK_RATES.get("XMR_USD", Decimal("150.0"))
        return amount * rate
    if token == "xEUR":
        # EUR to USD: approximate via XMR pivot
        eur_rate = FALLBACK_RATES.get("XMR_EUR", Decimal("138.0"))
        usd_rate = FALLBACK_RATES.get("XMR_USD", Decimal("150.0"))
        # 1 xEUR = (usd_rate / eur_rate) USD
        if eur_rate > 0:
            return amount * (usd_rate / eur_rate)
        return amount
    # Unknown token -- treat as zero
    return Decimal("0")


def _calculate_allocation_pct(
    balances: Dict[str, Decimal],
    total_usd: Decimal,
) -> Dict[str, int]:
    """Return allocation percentages (rounded int) for each token."""
    if total_usd <= Decimal("0"):
        return {}
    result: Dict[str, int] = {}
    for token, amount in balances.items():
        usd_val = _token_usd_value(token, amount)
        pct = int((usd_val / total_usd * Decimal("100")).to_integral_value())
        if pct > 0 or amount > 0:
            result[token] = pct
    return result


def _policy_to_dict(policy) -> Dict[str, Any]:
    """Convert a TreasuryPolicy ORM object to an immutable response dict."""
    cooldown_minutes = (policy.rebalance_cooldown_secs or 300) // 60
    return {
        "target_allocation": policy.target_allocation,
        "rebalance_threshold_pct": policy.rebalance_threshold_pct,
        "cooldown_minutes": cooldown_minutes,
        "emergency_reserve_pct": policy.emergency_reserve_pct,
        "is_active": policy.is_active,
        "last_rebalance_at": (
            policy.last_rebalance_at.isoformat() if policy.last_rebalance_at else None
        ),
    }


def _rebalance_log_to_dict(log) -> Dict[str, Any]:
    """Convert a TreasuryRebalanceLog ORM object to a response dict."""
    return {
        "id": str(log.id),
        "trigger": log.trigger,
        "conversions": log.conversions,
        "pre_allocation": log.pre_allocation,
        "post_allocation": log.post_allocation,
        "total_value_xusd": str(log.total_value_xusd),
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class TreasuryService:
    """Agent treasury management operations."""

    def set_policy(
        self,
        db: Session,
        agent_id: UUID,
        allocation: Dict[str, int],
        rebalance_threshold_pct: int = 5,
        cooldown_minutes: int = 60,
        emergency_reserve_pct: int = 10,
    ) -> Dict[str, Any]:
        """Validate allocation and upsert the treasury policy.

        Parameters
        ----------
        allocation : dict
            Mapping of token -> percentage. Values must sum to 100.

        Returns
        -------
        dict with policy details.

        Raises
        ------
        ValueError
            If allocation is empty, contains negative values, or does not sum to 100.
        """
        if not allocation:
            raise ValueError("Allocation must not be empty")

        for token, pct in allocation.items():
            if pct < 0:
                raise ValueError(
                    f"Allocation for {token} must be non-negative, got {pct}"
                )

        total = sum(allocation.values())
        if total != 100:
            raise ValueError(
                f"Allocation values must sum to 100, got {total}"
            )

        repo = TreasuryRepository(db)
        cooldown_secs = cooldown_minutes * 60

        policy = repo.set_policy(
            agent_id=agent_id,
            target_allocation=allocation,
            rebalance_threshold_pct=rebalance_threshold_pct,
            rebalance_cooldown_secs=cooldown_secs,
            emergency_reserve_pct=emergency_reserve_pct,
        )
        db.flush()

        return _policy_to_dict(policy)

    def get_policy(
        self, db: Session, agent_id: UUID
    ) -> Optional[Dict[str, Any]]:
        """Return the treasury policy dict or None."""
        repo = TreasuryRepository(db)
        policy = repo.get_policy(agent_id)
        if policy is None:
            return None
        return _policy_to_dict(policy)

    def deactivate_policy(self, db: Session, agent_id: UUID) -> None:
        """Deactivate the treasury policy for the given agent."""
        repo = TreasuryRepository(db)
        repo.deactivate_policy(agent_id)
        db.flush()

    def get_status(self, db: Session, agent_id: UUID) -> Dict[str, Any]:
        """Return current balances, allocation percentages, and total USD value."""
        rows = (
            db.query(AgentBalance)
            .filter(AgentBalance.agent_id == agent_id)
            .all()
        )

        balances: Dict[str, Decimal] = {}
        for row in rows:
            amount = row.available or Decimal("0")
            if amount > 0:
                balances[row.token] = amount

        total_usd = sum(
            _token_usd_value(token, amt)
            for token, amt in balances.items()
        )

        allocation_pct = _calculate_allocation_pct(balances, total_usd)

        return {
            "balances": {t: str(a) for t, a in balances.items()},
            "allocation_pct": allocation_pct,
            "total_value_xusd": str(total_usd),
        }

    def rebalance(
        self,
        db: Session,
        agent_id: UUID,
        trigger: str = "manual",
    ) -> Dict[str, Any]:
        """Calculate drift and execute rebalancing conversions if needed.

        Returns
        -------
        dict with keys: rebalanced (bool), reason (str if skipped),
        conversions (list of conversion results).
        """
        repo = TreasuryRepository(db)
        policy = repo.get_policy(agent_id)

        if policy is None or not policy.is_active:
            raise ValueError("No active treasury policy found for this agent")

        target = policy.target_allocation  # e.g. {"XMR": 50, "xUSD": 50}
        threshold = policy.rebalance_threshold_pct or 5
        cooldown_secs = policy.rebalance_cooldown_secs or 300
        emergency_reserve_pct = policy.emergency_reserve_pct or 10

        # Check cooldown
        if policy.last_rebalance_at is not None:
            cooldown_delta = timedelta(seconds=cooldown_secs)
            # Handle timezone-aware vs naive datetimes for SQLite compatibility
            last_rebalance = policy.last_rebalance_at
            now = datetime.now(timezone.utc)
            if last_rebalance.tzinfo is None:
                last_rebalance = last_rebalance.replace(tzinfo=timezone.utc)
            if now - last_rebalance < cooldown_delta:
                return {
                    "rebalanced": False,
                    "reason": "cooldown",
                    "conversions": [],
                }

        # Get current status
        status = self.get_status(db, agent_id)
        current_pct = status["allocation_pct"]
        total_usd = Decimal(status["total_value_xusd"])

        if total_usd <= Decimal("0"):
            return {
                "rebalanced": False,
                "reason": "no_balance",
                "conversions": [],
            }

        # Calculate max drift
        max_drift = Decimal("0")
        for token, target_pct in target.items():
            current = Decimal(str(current_pct.get(token, 0)))
            drift = abs(current - Decimal(str(target_pct)))
            if drift > max_drift:
                max_drift = drift

        if max_drift < Decimal(str(threshold)):
            return {
                "rebalanced": False,
                "reason": "below_threshold",
                "conversions": [],
            }

        # Determine pre-allocation snapshot
        pre_allocation = dict(current_pct)

        # Plan conversions: identify tokens that are over-allocated and under-allocated
        over: List[tuple] = []   # (token, excess_usd)
        under: List[tuple] = []  # (token, deficit_usd)

        for token, target_pct in target.items():
            target_usd = total_usd * Decimal(str(target_pct)) / Decimal("100")
            current_token_pct = Decimal(str(current_pct.get(token, 0)))
            current_usd = total_usd * current_token_pct / Decimal("100")
            diff = current_usd - target_usd

            if diff > Decimal("0.01"):
                over.append((token, diff))
            elif diff < Decimal("-0.01"):
                under.append((token, abs(diff)))

        # Apply emergency reserve constraint: ensure XMR balance does not drop
        # below emergency_reserve_pct of total value
        reserve_usd = total_usd * Decimal(str(emergency_reserve_pct)) / Decimal("100")
        xmr_rate = FALLBACK_RATES.get("XMR_USD", Decimal("150.0"))

        # Execute conversions
        conv_svc = ConversionService()
        conversions: List[Dict[str, Any]] = []

        for from_token, excess_usd in over:
            for to_token, deficit_usd in under:
                convert_usd = min(excess_usd, deficit_usd)
                if convert_usd < Decimal("0.01"):
                    continue

                # Apply emergency reserve: limit XMR sell
                if from_token == "XMR":
                    # Current XMR balance
                    bal_repo = BalanceRepository(db)
                    xmr_bal = bal_repo.get_available(agent_id, "XMR")
                    xmr_usd_val = xmr_bal * xmr_rate
                    max_sell_usd = xmr_usd_val - reserve_usd
                    if max_sell_usd <= Decimal("0"):
                        continue
                    convert_usd = min(convert_usd, max_sell_usd)

                # Determine the amount in the from_token
                if from_token == "XMR":
                    amount = convert_usd / xmr_rate
                elif from_token == "xUSD":
                    amount = convert_usd
                elif from_token == "xEUR":
                    eur_rate = FALLBACK_RATES.get("XMR_EUR", Decimal("138.0"))
                    usd_rate = FALLBACK_RATES.get("XMR_USD", Decimal("150.0"))
                    amount = convert_usd * eur_rate / usd_rate if usd_rate > 0 else convert_usd
                else:
                    continue

                if amount <= Decimal("0"):
                    continue

                # Determine the valid conversion pair
                # Only XMR<->xUSD and XMR<->xEUR are supported
                if from_token == "XMR" and to_token in ("xUSD", "xEUR"):
                    pass
                elif from_token in ("xUSD", "xEUR") and to_token == "XMR":
                    pass
                elif from_token == "xUSD" and to_token == "xEUR":
                    # Route through XMR: xUSD -> XMR -> xEUR
                    # Skip for simplicity in v1; only direct pairs
                    continue
                elif from_token == "xEUR" and to_token == "xUSD":
                    continue
                else:
                    continue

                try:
                    result = conv_svc.convert(
                        db, agent_id, from_token, to_token, amount,
                    )
                    conversions.append(result)
                except (ValueError, Exception) as exc:
                    logger.warning(
                        "Treasury rebalance conversion failed for agent %s: %s",
                        agent_id, exc,
                    )

        if not conversions:
            return {
                "rebalanced": False,
                "reason": "no_conversions_needed",
                "conversions": [],
            }

        # Post-rebalance allocation
        post_status = self.get_status(db, agent_id)
        post_allocation = post_status["allocation_pct"]
        post_total_usd = Decimal(post_status["total_value_xusd"])

        # Log the rebalance
        repo.add_rebalance_log(
            agent_id=agent_id,
            trigger=trigger,
            conversions=conversions,
            pre_allocation=pre_allocation,
            post_allocation=post_allocation,
            total_value_xusd=post_total_usd,
        )

        # Update last_rebalance_at
        repo.update_last_rebalance(agent_id)
        db.flush()

        return {
            "rebalanced": True,
            "conversions": conversions,
            "pre_allocation": pre_allocation,
            "post_allocation": post_allocation,
            "total_value_xusd": str(post_total_usd),
        }

    def get_history(
        self,
        db: Session,
        agent_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return rebalance log entries for the agent."""
        repo = TreasuryRepository(db)
        logs = repo.list_rebalance_history(agent_id, limit=limit, offset=offset)
        return [_rebalance_log_to_dict(log) for log in logs]
