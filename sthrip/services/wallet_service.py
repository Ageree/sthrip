"""
WalletService — wrapper for hub wallet operations via MoneroWalletRPC.

Handles deposit address generation, withdrawals, incoming transfer
retrieval, and wallet info for health checks.
"""

import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, List, Optional
from uuid import UUID

from ..db.repository import BalanceRepository
from ..wallet import MoneroWalletRPC
from .wallet_cache import WalletRPCCache


# ═══════════════════════════════════════════════════════════════════════════════
# PICONERO CONVERSION
# ═══════════════════════════════════════════════════════════════════════════════

PICONERO = Decimal("1000000000000")  # 1 XMR = 1e12 piconero


def xmr_to_piconero(xmr: Decimal) -> int:
    """Convert XMR amount to piconero (atomic units)."""
    if xmr < 0:
        raise ValueError("Amount must not be negative")
    return int(xmr * PICONERO)


def piconero_to_xmr(piconero: int) -> Decimal:
    """Convert piconero (atomic units) to XMR amount."""
    return Decimal(str(piconero)) / PICONERO


# ═══════════════════════════════════════════════════════════════════════════════
# WALLET SERVICE
# ═══════════════════════════════════════════════════════════════════════════════

class WalletService:
    """Manages hub wallet operations through MoneroWalletRPC.

    Uses a single account (account_index=0) with unique subaddresses
    per agent for deposit tracking.
    """

    def __init__(
        self,
        wallet_rpc: MoneroWalletRPC,
        db_session_factory: Callable,
        account_index: int = 0,
    ):
        self.wallet = WalletRPCCache(wallet_rpc)
        self._raw_rpc = wallet_rpc
        self._db_session_factory = db_session_factory
        self._account_index = account_index
        self._hub_addr_cache = None
        self._hub_addr_cache_time = 0.0
        self._hub_addr_cache_ttl = 300  # 5 minutes
        self._hub_addr_lock = threading.Lock()
        self._deposit_addr_locks: Dict[UUID, threading.Lock] = {}
        self._deposit_addr_locks_guard = threading.Lock()

    @classmethod
    def from_env(cls, db_session_factory: Callable) -> "WalletService":
        """Create WalletService from environment variables."""
        return cls(
            wallet_rpc=MoneroWalletRPC.from_env(),
            db_session_factory=db_session_factory,
        )

    def _get_agent_lock(self, agent_id: UUID) -> threading.Lock:
        """Get or create a per-agent lock for deposit address creation."""
        with self._deposit_addr_locks_guard:
            if agent_id not in self._deposit_addr_locks:
                self._deposit_addr_locks[agent_id] = threading.Lock()
            return self._deposit_addr_locks[agent_id]

    def get_or_create_deposit_address(self, agent_id: UUID) -> str:
        """Get or create a unique subaddress for an agent.

        Uses a per-agent lock to prevent race conditions where two concurrent
        requests create duplicate subaddresses.

        1. Check AgentBalance.deposit_address in DB
        2. If missing — create via wallet RPC with label=agent_id
        3. Save to DB via BalanceRepository.set_deposit_address()
        4. Return the subaddress
        """
        agent_lock = self._get_agent_lock(agent_id)
        with agent_lock:
            with self._db_session_factory() as db:
                repo = BalanceRepository(db)
                balance = repo.get_or_create(agent_id)

                if balance.deposit_address:
                    return balance.deposit_address

                result = self.wallet.create_address(
                    account_index=self._account_index,
                    label=str(agent_id),
                )
                address = result["address"]
                repo.set_deposit_address(agent_id, address)
                return address

    def send_withdrawal(self, to_address: str, amount: Decimal) -> Dict:
        """Send XMR from hub wallet to external address.

        Returns dict with tx_hash, fee (XMR), and amount (XMR).
        Raises ValueError if to_address is the hub wallet's own address.
        Raises WalletRPCError on RPC failure.
        """
        # Reject self-sends to prevent accounting discrepancies
        hub_addresses = self._get_hub_addresses()
        if to_address in hub_addresses:
            raise ValueError(
                "Cannot withdraw to hub wallet's own address — "
                "self-send would create an accounting discrepancy"
            )

        # wallet.transfer() handles XMR->piconero conversion internally
        result = self.wallet.transfer(
            destination=to_address,
            amount=amount,
        )
        return {
            "tx_hash": result["tx_hash"],
            "fee": piconero_to_xmr(result.get("fee", 0)),
            "amount": amount,
        }

    def _get_hub_addresses(self) -> frozenset:
        """Return all known hub wallet addresses (primary + subaddresses).

        Results are cached for _hub_addr_cache_ttl seconds to avoid
        an RPC call on every withdrawal.  Thread-safe via _hub_addr_lock.
        """
        with self._hub_addr_lock:
            now = time.monotonic()
            if self._hub_addr_cache is not None and (now - self._hub_addr_cache_time) < self._hub_addr_cache_ttl:
                return self._hub_addr_cache

            addr_data = self.wallet.get_address(self._account_index)
            addresses = {addr_data["address"]}
            for entry in addr_data.get("addresses", []):
                addresses.add(entry["address"])
            result = frozenset(addresses)

            self._hub_addr_cache = result
            self._hub_addr_cache_time = now
            return result

    def get_incoming_transfers(self, min_height: int = 0) -> List[Dict]:
        """Get incoming transfers from wallet RPC.

        Uses incoming_transfers RPC (unspent outputs) which captures
        self-transfers within the same wallet, unlike get_transfers.

        Returns list of dicts with amounts converted to XMR.
        """
        raw = self.wallet.incoming_transfers(
            transfer_type="all",
            account_index=self._account_index,
        )

        # Get current height for confirmation count and height filtering
        current_height = self.wallet.get_height()

        # Build subaddress index -> address map (single RPC call)
        subaddr_map = self._build_subaddress_map()

        result = []
        for tx in raw:
            height = tx.get("block_height", 0)
            if min_height > 0 and height < min_height:
                continue

            confirmations = (current_height - height) if height > 0 else 0

            # Resolve subaddress to address string for agent matching
            subaddr = tx.get("subaddr_index", {})
            minor = subaddr.get("minor", 0)
            if minor == 0:
                # Primary address — skip (not a deposit subaddress)
                continue

            address = subaddr_map.get(minor)
            if not address:
                continue

            result.append({
                "txid": tx["tx_hash"],
                "amount": piconero_to_xmr(tx["amount"]),
                "confirmations": confirmations,
                "height": height,
                "subaddr_index": subaddr,
                "address": address,
            })
        return result

    def _build_subaddress_map(self) -> Dict[int, str]:
        """Build mapping of subaddress minor index -> address string."""
        addr_data = self.wallet.get_address(self._account_index)
        return {
            addr["address_index"]: addr["address"]
            for addr in addr_data.get("addresses", [])
        }

    def get_outgoing_transfers(self, min_height: Optional[int] = None) -> List[Dict]:
        """Get outgoing transfers from wallet RPC.

        Used by withdrawal recovery to match pending withdrawals
        against on-chain transactions.

        Returns list of dicts with: tx_hash, amount (XMR), fee (XMR),
        address, timestamp, height.
        """
        raw = self.wallet.get_transfers(
            incoming=False,
            outgoing=True,
            pending=True,
            min_height=min_height,
        )

        result = []
        for tx in raw.get("out", []):
            timestamp = tx.get("timestamp")
            result.append({
                "tx_hash": tx.get("txid", tx.get("tx_hash", "")),
                "amount": piconero_to_xmr(tx.get("amount", 0)),
                "fee": piconero_to_xmr(tx.get("fee", 0)),
                "address": tx.get("address", ""),
                "timestamp": (
                    datetime.fromtimestamp(timestamp, tz=timezone.utc)
                    if timestamp
                    else None
                ),
                "height": tx.get("height", 0),
            })

        for tx in raw.get("pending", []):
            result.append({
                "tx_hash": tx.get("txid", tx.get("tx_hash", "")),
                "amount": piconero_to_xmr(tx.get("amount", 0)),
                "fee": piconero_to_xmr(tx.get("fee", 0)),
                "address": tx.get("address", ""),
                "timestamp": None,
                "height": 0,
            })

        return result

    def get_wallet_info(self) -> Dict:
        """Get wallet balance and address info for health check / admin."""
        balance_data = self.wallet.get_balance(self._account_index)
        address_data = self.wallet.get_address(self._account_index)
        return {
            "balance": piconero_to_xmr(balance_data["balance"]),
            "unlocked_balance": piconero_to_xmr(balance_data["unlocked_balance"]),
            "address": address_data["address"],
        }
