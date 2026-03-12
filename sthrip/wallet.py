"""
Monero wallet RPC client
"""

import requests
from requests.auth import HTTPDigestAuth
import json
from typing import Optional, List, Dict, Any
from decimal import Decimal

from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, AsyncRetrying,
)

from .types import WalletInfo, Payment, PaymentStatus


class MoneroWalletRPC:
    """Client for monero-wallet-rpc"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18082,
        user: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 30,
        use_ssl: bool = False,
    ):
        scheme = "https" if use_ssl else "http"
        self.url = f"{scheme}://{host}:{port}/json_rpc"
        self.auth = HTTPDigestAuth(user, password) if user else None
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        self._async_client = None
    
    @classmethod
    def from_env(cls):
        """Create wallet RPC from centralized settings"""
        from sthrip.config import get_settings
        settings = get_settings()
        return cls(
            host=settings.monero_rpc_host,
            port=settings.monero_rpc_port,
            user=settings.monero_rpc_user,
            password=settings.monero_rpc_pass,
            timeout=settings.wallet_rpc_timeout,
        )

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
        amount: Decimal,
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
    
    _VALID_TRANSFER_TYPES = frozenset({"all", "available", "unavailable"})

    def incoming_transfers(
        self,
        transfer_type: str = "all",
        account_index: int = 0,
        subaddr_indices: Optional[List[int]] = None,
    ) -> List[Dict]:
        """Get incoming transfers (unspent outputs) per subaddress.

        Unlike get_transfers, this includes self-transfers within the same wallet.
        """
        if transfer_type not in self._VALID_TRANSFER_TYPES:
            raise ValueError(
                f"transfer_type must be one of: {sorted(self._VALID_TRANSFER_TYPES)}"
            )
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
    
    _ALLOWED_KEY_TYPES = frozenset({"view_key"})

    def query_key(self, key_type: str) -> Dict:
        """Query wallet key (only view_key allowed for safety)."""
        if key_type not in self._ALLOWED_KEY_TYPES:
            raise ValueError(
                f"query_key only allows: {sorted(self._ALLOWED_KEY_TYPES)}"
            )
        return self._call("query_key", {"key_type": key_type})

    def label_address(self, index: Dict, label: str) -> None:
        """Label a subaddress"""
        self._call("label_address", {
            "index": index,
            "label": label
        })


    async def _get_async_client(self):
        """Get or create persistent async httpx client."""
        import httpx
        if self._async_client is None or self._async_client.is_closed:
            auth = None
            if self.auth:
                auth = httpx.DigestAuth(self.auth.username, self.auth.password)
            self._async_client = httpx.AsyncClient(
                timeout=self.timeout,
                auth=auth,
                headers=self.headers,
            )
        return self._async_client

    async def aclose(self):
        """Close the persistent async HTTP client."""
        if self._async_client is not None and not self._async_client.is_closed:
            await self._async_client.close()
        self._async_client = None

    async def _acall(self, method: str, params: Optional[Dict] = None) -> Any:
        """Async JSON-RPC call with retry on transient errors."""
        try:
            import httpx
        except ImportError:
            import asyncio
            return await asyncio.to_thread(self._call, method, params)

        payload = {
            "jsonrpc": "2.0",
            "id": "0",
            "method": method,
            "params": params or {},
        }

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.1, min=0.1, max=2),
            retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
            reraise=True,
        ):
            with attempt:
                client = await self._get_async_client()
                response = await client.post(self.url, json=payload)
                response.raise_for_status()

                result = response.json()
                if "error" in result:
                    raise WalletRPCError(result["error"]["message"])

                return result.get("result")

    async def async_get_balance(self, account_index: int = 0) -> Dict[str, int]:
        """Async version of get_balance."""
        return await self._acall("get_balance", {"account_index": account_index})

    async def async_get_height(self) -> int:
        """Async version of get_height."""
        result = await self._acall("get_height")
        return result["height"]

    async def async_get_address(self, account_index: int = 0) -> Dict:
        """Async version of get_address."""
        return await self._acall("get_address", {"account_index": account_index})

    async def async_incoming_transfers(
        self,
        transfer_type: str = "all",
        account_index: int = 0,
        subaddr_indices: Optional[List[int]] = None,
    ) -> List[Dict]:
        """Async version of incoming_transfers."""
        if transfer_type not in self._VALID_TRANSFER_TYPES:
            raise ValueError(
                f"transfer_type must be one of: {sorted(self._VALID_TRANSFER_TYPES)}"
            )
        params = {
            "transfer_type": transfer_type,
            "account_index": account_index,
        }
        if subaddr_indices is not None:
            params["subaddr_indices"] = subaddr_indices
        result = await self._acall("incoming_transfers", params)
        return result.get("transfers", [])

    async def async_transfer(
        self,
        destination: str,
        amount: Decimal,
        priority: int = 2,
        mixin: int = 10,
        payment_id: Optional[str] = None,
    ) -> Dict:
        """Async version of transfer."""
        atomic_amount = int(Decimal(str(amount)) * Decimal("1000000000000"))
        destinations = [{"address": destination, "amount": atomic_amount}]
        params = {
            "destinations": destinations,
            "priority": priority,
            "ring_size": mixin + 1,
            "get_tx_key": True,
        }
        if payment_id:
            params["payment_id"] = payment_id
        return await self._acall("transfer", params)


class WalletRPCError(Exception):
    """Wallet RPC error"""
    pass
