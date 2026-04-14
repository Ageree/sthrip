"""
DepositMonitor — background worker that polls monero-wallet-rpc
for incoming transfers and credits agent balances after confirmation.
"""

import asyncio
import logging
import uuid as _uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, List, Optional
from uuid import UUID

from sqlalchemy import text

from ..db.models import AgentBalance, TransactionStatus, PaymentType
from ..db.repository import BalanceRepository, TransactionRepository, SystemStateRepository
from sthrip.config import get_settings
from .wallet_service import WalletService

try:
    import redis as _redis_lib
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

_HEIGHT_KEY = "last_scanned_height"
_LOCK_KEY = "deposit_monitor:poll_lock"
_PG_ADVISORY_LOCK_ID = 73910  # arbitrary unique int for advisory lock
_VALID_NETWORKS = frozenset({"mainnet", "stagenet", "testnet"})
_MAX_BACKOFF = 300
_ALERT_THRESHOLD = 5

logger = logging.getLogger(__name__)


class DepositMonitor:
    """Polls wallet RPC for incoming deposits and credits balances.

    Tracks confirmation counts, updates pending/available balances,
    and fires webhooks when deposits are fully confirmed.
    """

    def __init__(
        self,
        wallet_service: WalletService,
        db_session_factory: Callable,
        min_confirmations: int = 10,
        poll_interval: int = 30,
        webhook_fn: Optional[Callable] = None,
        network: Optional[str] = None,
    ):
        self.wallet = wallet_service
        self._db_session_factory = db_session_factory
        self.min_confirmations = min_confirmations
        self.poll_interval = poll_interval
        self._webhook_fn = webhook_fn
        self._running = False
        self._last_height = 0
        self._instance_id = str(_uuid.uuid4())
        self._redis = None

        if _REDIS_AVAILABLE:
            redis_url = get_settings().redis_url or None
            if redis_url:
                try:
                    self._redis = _redis_lib.from_url(redis_url, decode_responses=True)
                    self._redis.ping()
                except Exception:
                    logger.warning("Redis unavailable for deposit lock, using PG advisory lock")
                    self._redis = None

        resolved_network = network or get_settings().monero_network
        if resolved_network not in _VALID_NETWORKS:
            raise ValueError(
                f"Invalid MONERO_NETWORK: {resolved_network!r}. "
                f"Must be one of: {sorted(_VALID_NETWORKS)}"
            )
        self._network = resolved_network

    async def start(self) -> None:
        """Start the infinite polling loop."""
        self._running = True
        consecutive_failures = 0
        logger.info(
            "DepositMonitor started (min_confirmations=%d, poll_interval=%ds)",
            self.min_confirmations,
            self.poll_interval,
        )
        while self._running:
            try:
                await asyncio.to_thread(self.poll_once)
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                logger.exception(
                    "DepositMonitor poll error (consecutive=%d)",
                    consecutive_failures,
                )
                if consecutive_failures >= _ALERT_THRESHOLD:
                    logger.critical(
                        "DepositMonitor failed %d times — deposits may be missed",
                        consecutive_failures,
                    )
            backoff = min(
                self.poll_interval * max(consecutive_failures, 1),
                _MAX_BACKOFF,
            )
            await asyncio.sleep(backoff)

    def load_persisted_height(self) -> None:
        """Load last_scanned_height from SystemState DB."""
        with self._db_session_factory() as db:
            state_repo = SystemStateRepository(db)
            saved = state_repo.get(_HEIGHT_KEY)
            if saved is not None:
                try:
                    self._last_height = int(saved)
                    logger.info("Loaded persisted height: %d", self._last_height)
                except ValueError:
                    logger.warning(
                        "Corrupt last_scanned_height value %r, resetting to 0",
                        saved,
                    )

    def stop(self) -> None:
        """Signal the polling loop to stop."""
        self._running = False
        logger.info("DepositMonitor stopped")

    def poll_once(self) -> None:
        """Execute one poll cycle with distributed lock.

        Acquires a Redis SETNX lock (or PostgreSQL advisory lock as fallback)
        to ensure only one instance polls at a time.

        1. Acquire distributed lock
        2. Fetch incoming transfers from wallet service
        3. For each transfer, match subaddress -> agent
        4. Create or update Transaction records
        5. Credit balance when confirmations >= min_confirmations
        6. Update _last_height for incremental scanning
        """
        if self._redis:
            self._poll_with_redis_lock()
        else:
            self._poll_with_pg_lock()

    # Lua script for atomic compare-and-delete (safe Redlock release)
    _UNLOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""

    def _poll_with_redis_lock(self) -> None:
        """Poll with Redis SETNX distributed lock."""
        lock_ttl = self.poll_interval * 2
        acquired = self._redis.set(
            _LOCK_KEY, self._instance_id, nx=True, ex=lock_ttl,
        )
        if not acquired:
            logger.info("Another instance holds the poll lock, skipping")
            return
        try:
            self._do_poll()
        finally:
            # Atomic compare-and-delete to avoid releasing another instance's lock
            self._redis.eval(self._UNLOCK_SCRIPT, 1, _LOCK_KEY, self._instance_id)

    def _poll_with_pg_lock(self) -> None:
        """Poll with PostgreSQL advisory lock fallback.

        Falls back to no-lock for non-PostgreSQL databases (e.g. SQLite in tests).
        """
        with self._db_session_factory() as db:
            dialect = db.bind.dialect.name if db.bind else ""
            if dialect != "postgresql":
                # No advisory locks on SQLite etc — just poll directly
                self._do_poll_with_session(db)
                return

            acquired = db.execute(
                text("SELECT pg_try_advisory_lock(:id)"),
                {"id": _PG_ADVISORY_LOCK_ID},
            ).scalar()
            if not acquired:
                logger.info("Another instance holds the advisory lock, skipping")
                return
            try:
                self._do_poll_with_session(db)
            finally:
                db.execute(
                    text("SELECT pg_advisory_unlock(:id)"),
                    {"id": _PG_ADVISORY_LOCK_ID},
                )

    def _do_poll(self) -> None:
        """Core poll logic (acquires its own DB session)."""
        with self._db_session_factory() as db:
            self._do_poll_with_session(db)

    def _do_poll_with_session(self, db) -> None:
        """Core poll logic using an existing DB session."""
        logger.debug("DepositMonitor polling (last_height=%d)", self._last_height)
        transfers = self.wallet.get_incoming_transfers(
            min_height=self._last_height,
        )
        if not transfers:
            logger.info("DepositMonitor: no new transfers")
            return
        logger.info("DepositMonitor found %d transfer(s)", len(transfers))

        try:
            new_height, deferred_webhooks = self._process_transfers(db, transfers)
            db.commit()
            # Only update in-memory height AFTER successful commit
            if new_height > self._last_height:
                self._last_height = new_height
            # Fire webhooks only after commit so we never notify for rolled-back balances
            for agent_id, txid, amount in deferred_webhooks:
                self._fire_webhook(agent_id, txid, amount)
        except Exception:
            db.rollback()
            raise

    def _process_transfers(self, db, transfers: List[Dict]):
        """Process a batch of incoming transfers.

        Returns (max_height, deferred_webhooks) where deferred_webhooks is a
        list of (agent_id, txid, amount) tuples to fire after commit.
        """
        bal_repo = BalanceRepository(db)
        tx_repo = TransactionRepository(db)

        max_height = self._last_height
        deferred_webhooks: List[tuple] = []

        for transfer in transfers:
            txid = transfer["txid"]
            amount = transfer["amount"]
            confirmations = transfer.get("confirmations", 0)
            height = transfer.get("height", 0)
            address = transfer.get("address", "")

            # Track highest block seen
            if height > max_height:
                max_height = height

            # Map subaddress -> agent
            agent_id = self._match_subaddress_to_agent(db, address)
            if agent_id is None:
                continue

            # Check if transaction already exists
            existing_tx = tx_repo.get_by_hash(txid)

            if existing_tx is None:
                webhook = self._handle_new_transfer(
                    db, tx_repo, bal_repo, agent_id,
                    txid, amount, confirmations, height,
                )
            else:
                webhook = self._handle_existing_transfer(
                    db, tx_repo, bal_repo, agent_id, existing_tx,
                    confirmations, height,
                )
            if webhook is not None:
                deferred_webhooks.append(webhook)

        # Persist height to DB for crash recovery (will be rolled back with txn if commit fails)
        if max_height > 0:
            state_repo = SystemStateRepository(db)
            state_repo.set(_HEIGHT_KEY, str(max_height))

        return max_height, deferred_webhooks

    def _handle_new_transfer(
        self, db, tx_repo, bal_repo, agent_id,
        txid, amount, confirmations, height,
    ):
        """Handle a newly discovered transfer.

        Returns (agent_id, txid, amount) tuple for deferred webhook, or None.
        """
        from sqlalchemy.exc import IntegrityError

        is_confirmed = confirmations >= self.min_confirmations
        status = TransactionStatus.CONFIRMED if is_confirmed else TransactionStatus.PENDING

        # Wrap in savepoint so IntegrityError only rolls back THIS insert,
        # not the entire batch transaction.
        savepoint = db.begin_nested()
        try:
            tx_repo.create(
                tx_hash=txid,
                network=self._network,
                from_agent_id=None,
                to_agent_id=agent_id,
                amount=amount,
                token="XMR",
                payment_type=PaymentType.DEPOSIT,
                status=status,
            )
        except IntegrityError:
            # Duplicate tx_hash — roll back only the savepoint, not the outer tx.
            savepoint.rollback()
            logger.warning("Duplicate tx_hash %s — skipping (already processed)", txid)
            return None

        if is_confirmed:
            # Directly credit available balance (uses row lock internally)
            bal_repo.deposit(agent_id, amount)
            db.flush()
            return (agent_id, txid, amount)
        else:
            # Add to pending balance via public API (uses row lock internally)
            bal_repo.add_pending(agent_id, amount)
            db.flush()
            return None

    def _handle_existing_transfer(
        self, db, tx_repo, bal_repo, agent_id, existing_tx,
        confirmations, height,
    ):
        """Update an existing transfer's confirmation count.

        Returns (agent_id, txid, amount) tuple for deferred webhook, or None.
        """
        # Update confirmations
        existing_tx.confirmations = confirmations

        if height:
            existing_tx.block_number = height

        # Check if newly confirmed
        was_pending = existing_tx.status == TransactionStatus.PENDING
        is_now_confirmed = confirmations >= self.min_confirmations

        webhook = None
        if was_pending and is_now_confirmed:
            existing_tx.status = TransactionStatus.CONFIRMED
            existing_tx.confirmed_at = datetime.now(tz=timezone.utc)

            # Move from pending to available (row-locked)
            amount = existing_tx.amount
            bal_repo.deposit(agent_id, amount)
            bal_repo.clear_pending_on_confirm(agent_id, amount)

            webhook = (agent_id, existing_tx.tx_hash, amount)

        db.flush()
        return webhook

    def _match_subaddress_to_agent(self, db, address: str) -> Optional[UUID]:
        """Look up agent_id by deposit_address in AgentBalance table."""
        balance = db.query(AgentBalance).filter(
            AgentBalance.deposit_address == address
        ).first()
        if balance:
            return balance.agent_id
        return None

    def _fire_webhook(self, agent_id: UUID, tx_hash: str, amount: Decimal) -> None:
        """Fire deposit confirmed webhook if handler is configured."""
        if self._webhook_fn:
            self._webhook_fn(
                str(agent_id),
                "payment.deposit_confirmed",
                {
                    "tx_hash": tx_hash,
                    "amount": str(amount),
                    "token": "XMR",
                },
            )
