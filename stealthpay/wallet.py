"""
Monero wallet RPC client
"""

import requests
import json
from typing import Optional, List, Dict, Any
from decimal import Decimal

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
        self.auth = (user, password) if user else None
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
    
    def _call(self, method: str, params: Optional[Dict] = None) -> Any:
        """Make JSON-RPC call"""
        payload = {
            "jsonrpc": "2.0",
            "id": "0",
            "method": method,
            "params": params or {}
        }
        
        try:
            response = requests.post(
                self.url,
                headers=self.headers,
                json=payload,
                auth=self.auth,
                timeout=self.timeout
            )
            response.raise_for_status()
            
            result = response.json()
            if "error" in result:
                raise WalletRPCError(result["error"]["message"])
            
            return result.get("result")
        except requests.exceptions.ConnectionError:
            raise WalletRPCError(
                f"Cannot connect to monero-wallet-rpc at {self.url}. "
                "Make sure wallet is running."
            )
    
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
    
    def get_address_index(self, address: str) -> Dict:
        """Get index of subaddress"""
        return self._call("get_address_index", {"address": address})
    
    def label_address(self, index: Dict, label: str) -> None:
        """Label a subaddress"""
        self._call("label_address", {
            "index": index,
            "label": label
        })


class WalletRPCError(Exception):
    """Wallet RPC error"""
    pass
