"""
Monero wallet RPC client
"""

import requests
from requests.auth import HTTPDigestAuth
import json
from typing import Optional, List, Dict, Any
from decimal import Decimal

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .types import WalletInfo, Payment, PaymentStatus


class MoneroWalletRPC:
    """Client for monero-wallet-rpc"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18082,
        user: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 30
    ):
        self.url = f"http://{host}:{port}/json_rpc"
        self.auth = HTTPDigestAuth(user, password) if user else None
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
    
    @classmethod
    def from_env(cls):
        """Create wallet RPC from environment variables"""
        import os
        host = os.environ.get("MONERO_RPC_HOST", "127.0.0.1")
        port = int(os.environ.get("MONERO_RPC_PORT", "18082"))
        user = os.environ.get("MONERO_RPC_USER", "")
        password = os.environ.get("MONERO_RPC_PASS", "")
        return cls(host=host, port=port, user=user, password=password)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
        reraise=True,
    )
    def _call(self, method: str, params: Optional[Dict] = None) -> Any:
        """Make JSON-RPC call with automatic retry on transient network errors.

        ConnectionError and Timeout propagate to tenacity for retry.
        After all retries exhausted, they are wrapped in WalletRPCError
        via the caller or the final reraise.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": "0",
            "method": method,
            "params": params or {}
        }

        response = requests.post(
            self.url,
            headers=self.headers,
            json=payload,
            auth=self.auth,
            timeout=self.timeout,
        )
        response.raise_for_status()

        result = response.json()
        if "error" in result:
            raise WalletRPCError(result["error"]["message"])

        return result.get("result")
    
    # === Wallet Info ===
    
    def get_balance(self, account_index: int = 0) -> Dict[str, int]:
        """Get wallet balance in atomic units"""
        return self._call("get_balance", {"account_index": account_index})
    
    def get_address(self, account_index: int = 0) -> Dict:
        """Get wallet address"""
        return self._call("get_address", {"account_index": account_index})
    
    def get_height(self) -> int:
        """Get current blockchain height"""
        result = self._call("get_height")
        return result["height"]
    
    # === Transfers ===
    
    def transfer(
        self,
        destination: str,
        amount: float,
        priority: int = 2,  # 0-4, 2 = normal
        mixin: int = 10,    # Ring size - higher = more private
        payment_id: Optional[str] = None
    ) -> Dict:
        """Send XMR to address"""
        # Convert XMR to atomic units (1 XMR = 10^12)
        atomic_amount = int(Decimal(str(amount)) * Decimal("1000000000000"))
        
        destinations = [{
            "address": destination,
            "amount": atomic_amount
        }]
        
        params = {
            "destinations": destinations,
            "priority": priority,
            "ring_size": mixin + 1,
            "get_tx_key": True
        }
        
        if payment_id:
            params["payment_id"] = payment_id
        
        return self._call("transfer", params)
    
    def get_transfers(
        self,
        incoming: bool = True,
        outgoing: bool = True,
        pending: bool = True,
        failed: bool = False,
        pool: bool = True,
        min_height: Optional[int] = None
    ) -> Dict:
        """Get transfer history"""
        params = {
            "in": incoming,
            "out": outgoing,
            "pending": pending,
            "failed": failed,
            "pool": pool
        }
        if min_height is not None:
            params["filter_by_height"] = True
            params["min_height"] = min_height
        
        return self._call("get_transfers", params)
    
    def get_transfer_by_txid(self, txid: str) -> Dict:
        """Get specific transfer by txid"""
        return self._call("get_transfer_by_txid", {"txid": txid})
    
    # === Subaddresses (Stealth) ===
    
    def create_address(self, account_index: int = 0, label: str = "") -> Dict:
        """Create new subaddress (stealth address)"""
        return self._call("create_address", {
            "account_index": account_index,
            "label": label
        })
    
    def incoming_transfers(
        self,
        transfer_type: str = "all",
        account_index: int = 0,
        subaddr_indices: Optional[List[int]] = None,
    ) -> List[Dict]:
        """Get incoming transfers (unspent outputs) per subaddress.

        Unlike get_transfers, this includes self-transfers within the same wallet.
        """
        params = {
            "transfer_type": transfer_type,
            "account_index": account_index,
        }
        if subaddr_indices is not None:
            params["subaddr_indices"] = subaddr_indices
        result = self._call("incoming_transfers", params)
        return result.get("transfers", [])

    def get_address_index(self, address: str) -> Dict:
        """Get index of subaddress"""
        return self._call("get_address_index", {"address": address})
    
    def query_key(self, key_type: str) -> Dict:
        """Query wallet key (mnemonic, view_key, spend_key)"""
        return self._call("query_key", {"key_type": key_type})

    def label_address(self, index: Dict, label: str) -> None:
        """Label a subaddress"""
        self._call("label_address", {
            "index": index,
            "label": label
        })


class WalletRPCError(Exception):
    """Wallet RPC error"""
    pass
