"""Startup recovery for pending withdrawals (saga completion)."""
import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("sthrip")


def recover_pending_withdrawals(
    pw_repo,
    wallet_service=None,
    balance_repo=None,
    max_age_minutes: int = 5,
) -> int:
    """Scan stale pending withdrawals and reconcile with wallet.

    Returns the number of records recovered.
    """
    stale = pw_repo.get_stale_pending(max_age_minutes=max_age_minutes)
    if not stale:
        return 0

    logger.info("Found %d stale pending withdrawals, reconciling...", len(stale))

    # Get recent outgoing transfers from wallet (if available)
    outgoing = []
    if wallet_service is not None:
        try:
            outgoing = wallet_service.get_outgoing_transfers()
        except Exception as e:
            logger.error("Failed to fetch outgoing transfers for recovery: %s", e)
            return 0

    recovered = 0
    for pw in stale:
        tx_match = _find_matching_transfer(pw, outgoing)
        if tx_match:
            pw_repo.mark_completed(pw.id, tx_hash=tx_match["tx_hash"])
            logger.info(
                "Recovery: marked pw=%s as completed (tx=%s)",
                pw.id, tx_match["tx_hash"],
            )
        else:
            # No matching on-chain tx — do NOT auto-credit.
            # Mark as needs_review for manual investigation.
            pw_repo.mark_needs_review(
                pw.id,
                reason="No matching on-chain tx after max_age_minutes",
            )
            logger.critical(
                "HUMAN_ACTION_REQUIRED: pw=%s agent=%s amount=%.12f has no "
                "matching on-chain tx. DO NOT auto-credit.",
                pw.id, pw.agent_id, pw.amount,
            )
        recovered += 1

    return recovered


async def periodic_recovery_loop(
    interval_seconds: int = 300,
    max_age_minutes: int = 5,
    wallet_service=None,
):
    """Run withdrawal recovery periodically (default: every 5 minutes).

    Args:
        wallet_service: WalletService instance for matching on-chain txs.
            Passed from the call site (api/main_v2.py) to avoid a
            circular import from sthrip.services → api.helpers.
    """
    import asyncio
    from sthrip.db.database import get_db
    from sthrip.db.repository import PendingWithdrawalRepository, BalanceRepository

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            wallet_svc = wallet_service
            with get_db() as db:
                pw_repo = PendingWithdrawalRepository(db)
                bal_repo = BalanceRepository(db)
                recovered = recover_pending_withdrawals(
                    pw_repo=pw_repo,
                    wallet_service=wallet_svc,
                    balance_repo=bal_repo,
                    max_age_minutes=max_age_minutes,
                )
                if recovered:
                    logger.info("Periodic recovery: reconciled %d withdrawals", recovered)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Periodic withdrawal recovery error: %s", e)


def _find_matching_transfer(
    pw,
    outgoing: list,
    max_timestamp_delta_minutes: int = 30,
) -> Optional[dict]:
    """Find an outgoing transfer matching a pending withdrawal."""
    for tx in outgoing:
        if tx.get("address") != pw.address:
            continue
        tx_amount = Decimal(str(tx.get("amount", 0)))
        if abs(tx_amount - pw.amount) >= Decimal("0.000000000001"):
            continue
        # Reject if timestamp delta exceeds threshold
        tx_timestamp = tx.get("timestamp")
        if tx_timestamp and hasattr(pw, "created_at") and pw.created_at:
            delta = abs((tx_timestamp - pw.created_at).total_seconds())
            if delta > max_timestamp_delta_minutes * 60:
                continue
        return tx
    return None
