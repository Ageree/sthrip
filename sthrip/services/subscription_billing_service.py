"""Phase 2 Sprint 4 — XMR-native subscription billing service.

This module owns five operations:

* ``bill_pro_subscriptions(now, db)`` — runs on the 1st of each month at
  04:00 UTC and during the daily 04:30 UTC grace retry. Charges every
  paid-tier agent the prorated USD-equivalent in piconero. Idempotent
  per ``(agent_id, month_start, status='monthly_charge')``.
* ``handle_grace_expiry(now, db)`` — runs daily at 04:30 UTC. Finds
  every agent whose ``tier_grace_until`` is in the past and downgrades
  them to FREE.
* ``start_grace_period(agent_id, db, now)`` — internal helper invoked
  when a monthly billing attempt finds the balance insufficient.
* ``prorate_charge`` / ``compute_refund`` — pure math helpers.

Atomicity contract
------------------
Every charge / refund is a single DB transaction:

    BEGIN
      SELECT FOR UPDATE balance row       -- (skipped on SQLite)
      UPDATE balance
      INSERT agent_billing_history row
      UPDATE agents.tier_grace_until / agents.tier   (when applicable)
    COMMIT                                 (commit is the caller's job)

If anything raises before commit, the caller's outer transaction rolls
back the entire block.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Optional
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy.orm import Session

from sthrip.db.balance_repo import BalanceRepository
from sthrip.db.enums import AgentTier
from sthrip.db.models import Agent, AgentBillingHistory
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.xmr_rate_service import (
    get_xmr_usd_rate,
    usd_to_xmr_piconero,
)

logger = logging.getLogger("sthrip.subscription_billing")

# ─────────────────────────────────────────────────────────────────────────────
# Constants — owned exclusively by this module so other services see one
# canonical price table.
# ─────────────────────────────────────────────────────────────────────────────

VERIFIED_USD_MONTHLY: Final[Decimal] = Decimal("29")
PREMIUM_USD_MONTHLY: Final[Decimal] = Decimal("999")
FREE_USD_MONTHLY: Final[Decimal] = Decimal("0")
GRACE_PERIOD_DAYS: Final[int] = 7

_TIER_USD_PRICE = {
    AgentTier.FREE: FREE_USD_MONTHLY,
    AgentTier.VERIFIED: VERIFIED_USD_MONTHLY,
    AgentTier.PREMIUM: PREMIUM_USD_MONTHLY,
    AgentTier.ENTERPRISE: PREMIUM_USD_MONTHLY,
}

PAID_TIERS: Final[tuple[AgentTier, ...]] = (
    AgentTier.VERIFIED,
    AgentTier.PREMIUM,
    AgentTier.ENTERPRISE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Pure math helpers (deterministic, no DB)
# ─────────────────────────────────────────────────────────────────────────────


def prorate_charge(
    monthly_cost_usd: Decimal,
    day_of_month: int,
    days_in_month: int,
) -> Decimal:
    """Proration for a mid-month upgrade.

    Convention: an agent upgrading on ``day_of_month`` pays for the
    REMAINING calendar days of the month, *inclusive* of day_of_month.
    So day 15 of a 30-day month pays 16/30 (days 15..30 inclusive).

    Returns Decimal quantized to 2 decimal places, ROUND_HALF_UP.
    """
    if day_of_month < 1 or day_of_month > days_in_month:
        raise ValueError(
            f"day_of_month {day_of_month} out of range 1..{days_in_month}"
        )
    remaining = Decimal(days_in_month - day_of_month + 1)
    raw = monthly_cost_usd * remaining / Decimal(days_in_month)
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_refund(
    monthly_cost_usd: Decimal,
    day_of_month: int,
    days_in_month: int,
) -> Decimal:
    """Refund for a mid-month downgrade.

    The unused portion equals the proration that *would* have been charged
    if the agent were upgrading today — i.e. the days remaining in the
    month inclusive of today are the days they will not benefit from.
    """
    return prorate_charge(monthly_cost_usd, day_of_month, days_in_month)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────


def _month_start(now: datetime) -> date:
    return date(now.year, now.month, 1)


def _days_in_month(now: datetime) -> int:
    return calendar.monthrange(now.year, now.month)[1]


def _has_monthly_charge(db: Session, agent_id: UUID, ms: date) -> bool:
    """True if a successful ``monthly_charge`` row already exists.

    This is the idempotency anchor that prevents double-charging when the
    cron is re-run within the same UTC day (or by a second replica that
    didn't acquire the distributed lease).
    """
    return (
        db.query(AgentBillingHistory)
        .filter(
            AgentBillingHistory.agent_id == agent_id,
            AgentBillingHistory.month_start == ms,
            AgentBillingHistory.status == "monthly_charge",
        )
        .first()
        is not None
    )


def _post_charge_audit(
    *,
    agent_id: UUID,
    status: str,
    amount_usd: Decimal,
    amount_piconero: int,
    rate_applied: Decimal,
    db: Session,
) -> None:
    """Audit-log a billing event. Isolated for test injection.

    The atomic-billing test patches this function to raise — exercising
    the rollback contract for the WHOLE billing block (balance deduct +
    history insert).
    """
    try:
        audit_log(
            "subscription_billing",
            agent_id=agent_id,
            details={
                "status": status,
                "amount_usd": str(amount_usd),
                "amount_piconero": amount_piconero,
                "rate_applied": str(rate_applied),
            },
            db=db,
        )
    except Exception as exc:  # noqa: BLE001 — never let audit kill billing
        logger.warning("audit_log emit failed: %s", exc)


def _record_billing_event(
    db: Session,
    *,
    agent_id: UUID,
    month_start: date,
    amount_usd: Decimal,
    amount_piconero: int,
    rate_applied: Decimal,
    status: str,
    tier_at_event: str,
    now: datetime,
) -> AgentBillingHistory:
    row = AgentBillingHistory(
        agent_id=agent_id,
        month_start=month_start,
        amount_usd=amount_usd,
        amount_piconero=amount_piconero,
        rate_applied=rate_applied,
        status=status,
        tier_at_event=tier_at_event,
        created_at=now,
    )
    db.add(row)
    db.flush()
    return row


# ─────────────────────────────────────────────────────────────────────────────
# Public service surface
# ─────────────────────────────────────────────────────────────────────────────


def start_grace_period(
    agent_id: UUID,
    db: Session,
    *,
    now: Optional[datetime] = None,
    rate_applied: Optional[Decimal] = None,
    amount_usd: Optional[Decimal] = None,
    amount_piconero: Optional[int] = None,
    tier_at_event: Optional[str] = None,
    month_start: Optional[date] = None,
) -> None:
    """Mark an agent as in-grace and audit-log the event.

    Sets ``tier_grace_until = now + GRACE_PERIOD_DAYS``. Tier itself is
    PRESERVED — the middleware sees the future ``tier_grace_until`` and
    keeps treating the agent as paid until it expires. Once expired, the
    daily ``handle_grace_expiry`` cron flips ``tier`` to FREE.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    grace_until = now + timedelta(days=GRACE_PERIOD_DAYS)

    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if db_agent is None:
        return
    db_agent.tier_grace_until = grace_until
    db.flush()

    _record_billing_event(
        db,
        agent_id=agent_id,
        month_start=month_start or _month_start(now),
        amount_usd=amount_usd if amount_usd is not None else Decimal("0.00"),
        amount_piconero=amount_piconero or 0,
        rate_applied=rate_applied if rate_applied is not None else Decimal("0"),
        status="monthly_grace_started",
        tier_at_event=tier_at_event or (db_agent.tier.value if db_agent.tier else "free"),
        now=now,
    )
    _post_charge_audit(
        agent_id=agent_id,
        status="monthly_grace_started",
        amount_usd=amount_usd or Decimal("0.00"),
        amount_piconero=amount_piconero or 0,
        rate_applied=rate_applied or Decimal("0"),
        db=db,
    )


def bill_pro_subscriptions(now: datetime, db: Session) -> dict:
    """Charge every paid-tier agent for the current month.

    Idempotent — uses ``(agent_id, month_start, status='monthly_charge')``
    as the lock key. Re-running on the same day is a no-op for already-
    charged agents.

    Returns a summary dict ``{charged, grace_started, skipped}``.
    """
    summary = {"charged": 0, "grace_started": 0, "skipped": 0}
    ms = _month_start(now)

    rate = get_xmr_usd_rate(now=now)

    paid_agents = (
        db.query(Agent)
        .filter(
            Agent.is_active.is_(True),
            Agent.tier.in_(PAID_TIERS),
        )
        .all()
    )

    bal_repo = BalanceRepository(db)

    for agent in paid_agents:
        if _has_monthly_charge(db, agent.id, ms):
            summary["skipped"] += 1
            continue

        tier_value = agent.tier.value
        usd_price = _TIER_USD_PRICE.get(agent.tier, Decimal("0"))
        if usd_price <= Decimal("0"):
            summary["skipped"] += 1
            continue

        amount_pico = usd_to_xmr_piconero(usd_price, rate)
        amount_xmr = Decimal(amount_pico) / (Decimal(10) ** 12)

        # Snapshot balance INSIDE the same session for atomicity.
        try:
            bal_repo.deduct(agent.id, amount_xmr, token="XMR")
        except ValueError:
            # Insufficient balance: open a grace period instead of charging.
            start_grace_period(
                agent.id,
                db,
                now=now,
                rate_applied=rate,
                amount_usd=usd_price,
                amount_piconero=amount_pico,
                tier_at_event=tier_value,
                month_start=ms,
            )
            summary["grace_started"] += 1
            continue

        # Successful charge — clear any stale grace marker.
        if agent.tier_grace_until is not None:
            agent.tier_grace_until = None
            db.flush()

        _record_billing_event(
            db,
            agent_id=agent.id,
            month_start=ms,
            amount_usd=usd_price.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            amount_piconero=amount_pico,
            rate_applied=rate,
            status="monthly_charge",
            tier_at_event=tier_value,
            now=now,
        )

        # Audit hook (separately patchable for the atomic-tx test).
        _post_charge_audit(
            agent_id=agent.id,
            status="monthly_charge",
            amount_usd=usd_price,
            amount_piconero=amount_pico,
            rate_applied=rate,
            db=db,
        )

        summary["charged"] += 1

    return summary


def handle_grace_expiry(now: datetime, db: Session) -> dict:
    """Downgrade every agent whose grace period has expired.

    Runs after ``bill_pro_subscriptions`` so an agent who topped up just
    before this cron runs gets a successful retry first.
    """
    summary = {"downgraded": 0}

    expired = (
        db.query(Agent)
        .filter(
            Agent.is_active.is_(True),
            Agent.tier.in_(PAID_TIERS),
            Agent.tier_grace_until.isnot(None),
            Agent.tier_grace_until < now,
        )
        .all()
    )

    for agent in expired:
        old_tier = agent.tier.value if agent.tier else "free"
        agent.tier = AgentTier.FREE
        agent.tier_grace_until = None
        db.flush()

        _record_billing_event(
            db,
            agent_id=agent.id,
            month_start=_month_start(now),
            amount_usd=Decimal("0.00"),
            amount_piconero=0,
            rate_applied=Decimal("0"),
            status="monthly_grace_expired_downgrade",
            tier_at_event=old_tier,
            now=now,
        )

        _post_charge_audit(
            agent_id=agent.id,
            status="monthly_grace_expired_downgrade",
            amount_usd=Decimal("0.00"),
            amount_piconero=0,
            rate_applied=Decimal("0"),
            db=db,
        )

        summary["downgraded"] += 1

    return summary


# Helper exported for endpoint use cases
def record_upgrade_charge(
    db: Session,
    *,
    agent_id: UUID,
    amount_usd: Decimal,
    amount_piconero: int,
    rate_applied: Decimal,
    tier_at_event: str,
    now: datetime,
) -> AgentBillingHistory:
    """Insert an ``upgrade_charge`` row for a self-service mid-month upgrade."""
    return _record_billing_event(
        db,
        agent_id=agent_id,
        month_start=_month_start(now),
        amount_usd=amount_usd.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        amount_piconero=amount_piconero,
        rate_applied=rate_applied,
        status="upgrade_charge",
        tier_at_event=tier_at_event,
        now=now,
    )


def record_downgrade_refund(
    db: Session,
    *,
    agent_id: UUID,
    amount_usd: Decimal,
    amount_piconero: int,
    rate_applied: Decimal,
    tier_at_event: str,
    now: datetime,
) -> AgentBillingHistory:
    """Insert a ``downgrade_refund`` row."""
    return _record_billing_event(
        db,
        agent_id=agent_id,
        month_start=_month_start(now),
        amount_usd=amount_usd.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        amount_piconero=amount_piconero,
        rate_applied=rate_applied,
        status="downgrade_refund",
        tier_at_event=tier_at_event,
        now=now,
    )


__all__ = [
    "FREE_USD_MONTHLY",
    "GRACE_PERIOD_DAYS",
    "PAID_TIERS",
    "PREMIUM_USD_MONTHLY",
    "VERIFIED_USD_MONTHLY",
    "bill_pro_subscriptions",
    "compute_refund",
    "handle_grace_expiry",
    "prorate_charge",
    "record_downgrade_refund",
    "record_upgrade_charge",
    "start_grace_period",
]
