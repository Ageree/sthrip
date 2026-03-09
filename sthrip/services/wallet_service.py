"""
WalletService — wrapper for hub wallet operations via MoneroWalletRPC.

Handles deposit address generation, withdrawals, incoming transfer
retrieval, and wallet info for health checks.
"""

from decimal import Decimal
from typing import Callable, Dict, List, Optional
from uuid import UUID

from ..db.repository import BalanceRepository
from ..wallet import MoneroWalletRPC


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
        self.wallet = wallet_rpc
        self._db_session_factory = db_session_factory
        self._account_index = account_index

    @classmethod
    def from_env(cls, db_session_factory: Callable) -> "WalletService":
        """Create WalletService from environment variables."""
        return cls(
            wallet_rpc=MoneroWalletRPC.from_env(),
            db_session_factory=db_session_factory,
        )

    def get_or_create_deposit_address(self, agent_id: UUID) -> str:
        """Get or create a unique subaddress for an agent.

        1. Check AgentBalance.deposit_address in DB
        2. If missing — create via wallet RPC with label=agent_id
        3. Save to DB via BalanceRepository.set_deposit_address()
        4. Return the subaddress
        """
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
            db.commit()
            return address

    def send_withdrawal(self, to_address: str, amount: Decimal) -> Dict:
        """Send XMR from hub wallet to external address.

        Returns dict with tx_hash, fee (XMR), and amount (XMR).
        Raises WalletRPCError on failure.
        """
        piconero_amount = xmr_to_piconero(amount)
        result = self.wallet.transfer(
            destination=to_address,
            amount=piconero_amount,
        )
        return {
            "tx_hash": result["tx_hash"],
            "fee": piconero_to_xmr(result.get("fee", 0)),
            "amount": amount,
        }

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

    def get_wallet_info(self) -> Dict:
        """Get wallet balance and address info for health check / admin."""
        balance_data = self.wallet.get_balance(self._account_index)
        address_data = self.wallet.get_address(self._account_index)
        return {
            "balance": piconero_to_xmr(balance_data["balance"]),
            "unlocked_balance": piconero_to_xmr(balance_data["unlocked_balance"]),
            "address": address_data["address"],
        }
