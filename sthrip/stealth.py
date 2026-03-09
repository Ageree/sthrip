"""
Stealth address management for anonymous payments
Monero subaddresses provide one-time use addresses for each payment
"""

import time
from typing import Optional, List, Dict
from datetime import datetime, timezone
from dataclasses import asdict

from .types import StealthAddress
from .wallet import MoneroWalletRPC


class StealthAddressManager:
    """
    Manages one-time stealth addresses (subaddresses) for anonymous payments.
    Each payment uses a unique address, making blockchain analysis impossible.
    """
    
    def __init__(self, wallet_rpc: MoneroWalletRPC, account_index: int = 0):
        self.wallet = wallet_rpc
        self.account_index = account_index
        self._cache: Dict[int, StealthAddress] = {}
    
    def generate(
        self,
        label: Optional[str] = None,
        purpose: Optional[str] = None
    ) -> StealthAddress:
        """
        Generate new stealth address for receiving payment.
        Each call creates unique address - perfect for one-time payments.
        
        Args:
            label: Optional label for the address
            purpose: What this address is for (e.g., "payment-from-agent-42")
        
        Returns:
            StealthAddress object
        """
        full_label = label or ""
        if purpose:
            full_label = f"{full_label}:{purpose}" if full_label else purpose
        
        result = self.wallet.create_address(
            account_index=self.account_index,
            label=full_label
        )
        
        stealth = StealthAddress(
            address=result["address"],
            index=result["address_index"],
            label=label,
            created_at=datetime.now(timezone.utc),
            used=False
        )
        
        self._cache[stealth.index] = stealth
        return stealth
    
    def generate_batch(
        self,
        count: int,
        prefix: Optional[str] = None
    ) -> List[StealthAddress]:
        """
        Generate multiple stealth addresses at once.
        Useful for setting up payment channels.
        """
        addresses = []
        for i in range(count):
            label = f"{prefix}-{i}" if prefix else f"batch-{i}"
            addr = self.generate(label=label)
            addresses.append(addr)
        return addresses
    
    def mark_used(self, address: str) -> None:
        """Mark address as used (after receiving payment)"""
        try:
            result = self.wallet.get_address_index(address)
            index = result["index"]["minor"]
            if index in self._cache:
                self._cache[index].used = True
        except Exception:
            pass  # Address not found or not ours
    
    def is_ours(self, address: str) -> bool:
        """Check if address belongs to our wallet"""
        try:
            self.wallet.get_address_index(address)
            return True
        except Exception:
            return False
    
    def get_unused(self) -> List[StealthAddress]:
        """Get all unused stealth addresses"""
        return [addr for addr in self._cache.values() if not addr.used]
    
    def rotate(self, old_address: str, purpose: Optional[str] = None) -> StealthAddress:
        """
        Rotate to new address after use.
        Returns new stealth address, marks old as used.
        """
        self.mark_used(old_address)
        return self.generate(purpose=purpose or "rotated")
