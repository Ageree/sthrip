"""
Redis cache layer for MoneroWalletRPC read operations.

Caches get_balance, get_address, and get_height results to reduce
load on the single-threaded wallet-rpc process.

Write operations (transfer, create_address) always go directly
to wallet-rpc and invalidate relevant cache entries.
"""

import json
import logging
import time
from decimal import Decimal
from typing import Callable, Dict, List, Optional
from uuid import UUID

from ..config import get_settings
from ..wallet import MoneroWalletRPC

logger = logging.getLogger("sthrip.wallet_cache")

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# Cache TTLs in seconds
BALANCE_TTL = 10     # Balance changes with each block (~2 min)
ADDRESS_TTL = 300    # Addresses are immutable once created
HEIGHT_TTL = 5       # Height changes every ~2 min, keep short
TRANSFERS_TTL = 10   # Transfer list updates with deposits

CACHE_PREFIX = "wrpc:"


class WalletRPCCache:
    """Redis-backed cache for wallet RPC read operations.

    Falls back to direct RPC calls if Redis is unavailable.
    """

    def __init__(self, wallet_rpc: MoneroWalletRPC, redis_url: str = ""):
        self._rpc = wallet_rpc
        self._redis = None

        url = redis_url or get_settings().redis_url
        if url and REDIS_AVAILABLE:
            try:
                self._redis = redis.from_url(url, decode_responses=True)
                self._redis.ping()
                logger.info("WalletRPCCache: Redis connected")
            except Exception as e:
                logger.warning("WalletRPCCache: Redis unavailable (%s), no caching", e)
                self._redis = None

    @property
    def rpc(self) -> MoneroWalletRPC:
        """Direct access to underlying RPC for write operations."""
        return self._rpc

    def _cache_get(self, key: str) -> Optional[str]:
        if self._redis is None:
            return None
        try:
            return self._redis.get(f"{CACHE_PREFIX}{key}")
        except Exception:
            return None

    def _cache_set(self, key: str, value: str, ttl: int) -> None:
        if self._redis is None:
            return
        try:
            self._redis.setex(f"{CACHE_PREFIX}{key}", ttl, value)
        except Exception:
            pass

    def _cache_delete(self, *keys: str) -> None:
        if self._redis is None:
            return
        try:
            full_keys = [f"{CACHE_PREFIX}{k}" for k in keys]
            self._redis.delete(*full_keys)
        except Exception:
            pass

    # === Cached read operations ===

    def get_balance(self, account_index: int = 0) -> Dict:
        key = f"balance:{account_index}"
        cached = self._cache_get(key)
        if cached is not None:
            return json.loads(cached)

        result = self._rpc.get_balance(account_index)
        self._cache_set(key, json.dumps(result), BALANCE_TTL)
        return result

    def get_address(self, account_index: int = 0) -> Dict:
        key = f"address:{account_index}"
        cached = self._cache_get(key)
        if cached is not None:
            return json.loads(cached)

        result = self._rpc.get_address(account_index)
        self._cache_set(key, json.dumps(result), ADDRESS_TTL)
        return result

    def get_height(self) -> int:
        key = "height"
        cached = self._cache_get(key)
        if cached is not None:
            return int(cached)

        result = self._rpc.get_height()
        self._cache_set(key, str(result), HEIGHT_TTL)
        return result

    def incoming_transfers(
        self,
        transfer_type: str = "all",
        account_index: int = 0,
        subaddr_indices: Optional[List[int]] = None,
    ) -> List[Dict]:
        # Subaddr_indices makes cache key complex; skip caching if specified
        if subaddr_indices is not None:
            return self._rpc.incoming_transfers(transfer_type, account_index, subaddr_indices)

        key = f"incoming:{account_index}:{transfer_type}"
        cached = self._cache_get(key)
        if cached is not None:
            return json.loads(cached)

        result = self._rpc.incoming_transfers(transfer_type, account_index)
        self._cache_set(key, json.dumps(result), TRANSFERS_TTL)
        return result

    def get_address_index(self, address: str) -> Dict:
        key = f"addrindex:{address[:16]}"
        cached = self._cache_get(key)
        if cached is not None:
            return json.loads(cached)

        result = self._rpc.get_address_index(address)
        self._cache_set(key, json.dumps(result), ADDRESS_TTL)
        return result

    # === Write operations (pass-through + invalidate) ===

    def create_address(self, account_index: int = 0, label: str = "") -> Dict:
        result = self._rpc.create_address(account_index=account_index, label=label)
        self._cache_delete(f"address:{account_index}")
        return result

    def transfer(self, **kwargs) -> Dict:
        result = self._rpc.transfer(**kwargs)
        self._cache_delete("balance:0")
        return result

    # === Pass-through for other operations ===

    def get_transfers(self, **kwargs) -> Dict:
        return self._rpc.get_transfers(**kwargs)

    def get_transfer_by_txid(self, txid: str) -> Dict:
        return self._rpc.get_transfer_by_txid(txid)

    def query_key(self, key_type: str) -> Dict:
        return self._rpc.query_key(key_type)

    def label_address(self, index: Dict, label: str) -> None:
        return self._rpc.label_address(index, label)
