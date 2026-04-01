"""
ConversionService — business logic for hub-balance currency conversions.

Supported pairs: XMR <-> xUSD, XMR <-> xEUR.
Fee: 0.5% of gross to_amount.

Rate source: attempts to import RateService; falls back to FALLBACK_RATES.
"""

import logging
from decimal import Decimal
from typing import Dict
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.models import AgentBalance
from sthrip.db.balance_repo import BalanceRepository
from sthrip.db.conversion_repo import ConversionRepository

logger = logging.getLogger("sthrip")

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

SUPPORTED_CONVERSIONS = {"XMR_xUSD", "xUSD_XMR", "XMR_xEUR", "xEUR_XMR"}

_CONVERSION_FEE = Decimal("0.005")

# Fallback rates used when no live RateService is available (test-safe).
FALLBACK_RATES: Dict[str, Decimal] = {
    "XMR_USD": Decimal("150.0"),
    "XMR_EUR": Decimal("138.0"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pair_key(from_currency: str, to_currency: str) -> str:
    return f"{from_currency}_{to_currency}"


def _resolve_rate(from_currency: str, to_currency: str) -> Decimal:
    """Return the exchange rate for the given pair.

    Resolution order:
    1. RateService (if available)
    2. FALLBACK_RATES

    The rate is expressed as: to_amount = from_amount * rate.
    """
    try:
        from sthrip.services.rate_service import RateService  # type: ignore[import]
        svc = RateService()
        return svc.get_rate(from_currency, to_currency)
    except (ImportError, Exception) as exc:
        logger.debug("RateService unavailable (%s), using fallback rates", exc)

    # Derive rate from FALLBACK_RATES
    if from_currency == "XMR" and to_currency in ("xUSD", "USD"):
        return FALLBACK_RATES["XMR_USD"]
    if from_currency == "XMR" and to_currency in ("xEUR", "EUR"):
        return FALLBACK_RATES["XMR_EUR"]
    if from_currency in ("xUSD", "USD") and to_currency == "XMR":
        return Decimal("1") / FALLBACK_RATES["XMR_USD"]
    if from_currency in ("xEUR", "EUR") and to_currency == "XMR":
        return Decimal("1") / FALLBACK_RATES["XMR_EUR"]

    raise ValueError(
        f"Unsupported conversion pair: {from_currency} -> {to_currency}"
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ConversionService:
    """Hub-balance currency conversion operations."""

    def convert(
        self,
        db: Session,
        agent_id: UUID,
        from_currency: str,
        to_currency: str,
        amount: Decimal,
    ) -> Dict:
        """Convert *amount* of *from_currency* to *to_currency* for *agent_id*.

        Steps
        -----
        1. Validate pair is supported.
        2. Validate amount > 0.
        3. Determine rate.
        4. Calculate gross to_amount and fee (0.5%).
        5. Deduct *amount* from source balance (raises ValueError if insufficient).
        6. Credit *net_to* to target balance.
        7. Record CurrencyConversion.
        8. Return result dict (all amounts as Decimal).

        Returns
        -------
        dict with keys: from_currency, from_amount, to_currency, gross_to_amount,
        fee_amount, net_to_amount, rate.
        """
        pair = _pair_key(from_currency, to_currency)
        if pair not in SUPPORTED_CONVERSIONS:
            raise ValueError(
                f"Unsupported conversion pair: {from_currency} -> {to_currency}"
            )

        if amount <= Decimal("0"):
            raise ValueError("Conversion amount must be greater than zero")

        rate = _resolve_rate(from_currency, to_currency)
        gross_to = amount * rate
        fee = gross_to * _CONVERSION_FEE
        net_to = gross_to - fee

        bal_repo = BalanceRepository(db)

        # Deduct source — raises ValueError("Insufficient balance") if needed
        bal_repo.deduct(agent_id, amount, token=from_currency)

        # Credit target
        bal_repo.credit(agent_id, net_to, token=to_currency)

        # Record conversion
        conv_repo = ConversionRepository(db)
        conv_repo.create(
            agent_id=agent_id,
            from_currency=from_currency,
            from_amount=amount,
            to_currency=to_currency,
            to_amount=net_to,
            rate=rate,
            fee_amount=fee,
        )

        return {
            "from_currency": from_currency,
            "from_amount": str(amount),
            "to_currency": to_currency,
            "gross_to_amount": str(gross_to),
            "fee_amount": str(fee),
            "net_to_amount": str(net_to),
            "rate": str(rate),
        }

    def get_all_balances(self, db: Session, agent_id: UUID) -> Dict[str, str]:
        """Return all token balances for *agent_id* as a dict of token -> amount string.

        Returns an empty dict if the agent has no balance records.
        """
        rows = (
            db.query(AgentBalance)
            .filter(AgentBalance.agent_id == agent_id)
            .all()
        )
        return {row.token: str(row.available or Decimal("0")) for row in rows}
