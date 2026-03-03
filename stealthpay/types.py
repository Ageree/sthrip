"""
Type definitions for StealthPay
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, List
from datetime import datetime


class PaymentStatus(Enum):
    """Payment status"""
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"


@dataclass
class Payment:
    """Payment transaction"""
    tx_hash: str
    amount: float  # in XMR
    from_address: Optional[str]  # None for incoming stealth
    to_address: str
    status: PaymentStatus
    confirmations: int
    fee: float
    timestamp: datetime
    memo: Optional[str] = None
    
    @property
    def is_confirmed(self) -> bool:
        return self.confirmations >= 10  # Monero default


@dataclass
class WalletInfo:
    """Wallet information"""
    address: str
    primary_address: str
    balance: float
    unlocked_balance: float
    height: int  # Current blockchain height
    view_only: bool = False


@dataclass
class StealthAddress:
    """One-time stealth address (Monero subaddress)"""
    address: str
    index: int
    label: Optional[str] = None
    created_at: Optional[datetime] = None
    used: bool = False
