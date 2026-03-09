"""
Advanced privacy enhancements for Sthrip
Beyond basic Monero privacy
"""

import random
import time
import hashlib
from typing import Optional, List
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum


class TransactionTiming(Enum):
    """Timing strategies for transaction broadcasting"""
    IMMEDIATE = "immediate"      # Send now
    RANDOM_DELAY = "random"      # Random delay 1-60 min
    BATCH = "batch"              # Wait for batch
    NIGHT_TIME = "night"         # Send during low-activity hours


@dataclass
class PrivacyConfig:
    """Privacy configuration"""
    # Ring size (higher = better privacy, higher fee)
    min_mixin: int = 10          # Minimum ring members
    max_mixin: int = 20          # Maximum (randomized)
    
    # Timing
    timing_strategy: TransactionTiming = TransactionTiming.RANDOM_DELAY
    min_delay_minutes: int = 1
    max_delay_minutes: int = 60
    
    # Subaddress rotation
    auto_rotate_addresses: bool = True
    max_reuse_count: int = 1     # Never reuse addresses
    
    # Decoy outputs
    use_decoy_change: bool = True  # Send change to new address
    decoy_amount_variance: float = 0.01  # Add small random amount
    
    # Connection privacy
    use_tor: bool = False        # Disabled as requested
    use_proxy: bool = False      # Can use VPN/proxy
    proxy_url: Optional[str] = None


class PrivacyEnhancer:
    """
    Enhances transaction privacy beyond default Monero settings
    """
    
    def __init__(self, config: Optional[PrivacyConfig] = None):
        self.config = config or PrivacyConfig()
        self._used_addresses: set = set()
        self._pending_transactions: List[dict] = []
    
    def get_optimal_mixin(self) -> int:
        """
        Randomize mixin size to avoid fingerprinting.
        Different transactions = different ring sizes
        """
        return random.randint(
            self.config.min_mixin,
            self.config.max_mixin
        )
    
    def calculate_delay(self) -> int:
        """
        Calculate random delay before broadcasting.
        Prevents timing correlation attacks.
        """
        if self.config.timing_strategy == TransactionTiming.IMMEDIATE:
            return 0
        
        elif self.config.timing_strategy == TransactionTiming.RANDOM_DELAY:
            return random.randint(
                self.config.min_delay_minutes * 60,
                self.config.max_delay_minutes * 60
            )
        
        elif self.config.timing_strategy == TransactionTiming.NIGHT_TIME:
            # Calculate delay until 2-4 AM
            now = datetime.now()
            night_start = now.replace(hour=2, minute=0, second=0)
            if night_start < now:
                night_start += timedelta(days=1)
            
            delay = (night_start - now).total_seconds()
            # Add randomness 0-2 hours
            delay += random.randint(0, 7200)
            return int(delay)
        
        elif self.config.timing_strategy == TransactionTiming.BATCH:
            # Wait for batch to fill (implement external logic)
            return 3600  # Default 1 hour
        
        return 0
    
    def obfuscate_amount(self, amount: float) -> float:
        """
        Add tiny variance to amount to avoid exact amount fingerprinting.
        e.g., instead of exactly 0.5, send 0.50000123
        """
        if not self.config.decoy_amount_variance:
            return amount
        
        variance = random.uniform(
            -self.config.decoy_amount_variance,
            self.config.decoy_amount_variance
        )
        return amount + variance
    
    def should_rotate_address(self, address: str) -> bool:
        """Check if address should be rotated"""
        if address in self._used_addresses:
            return True
        self._used_addresses.add(address)
        return False
    
    def generate_decoy_output(self) -> Optional[float]:
        """
        Generate small decoy output to confuse analysis.
        Occasionally sends tiny amount to random address.
        """
        if random.random() < 0.1:  # 10% chance
            return random.uniform(0.0001, 0.001)
        return None
    
    def create_churn_transaction(
        self,
        amount: float,
        rounds: int = 3
    ) -> List[float]:
        """
        Create churn (send to self multiple times) to break chain analysis.
        
        Even in Monero, churn helps against statistical analysis:
        - Round 1: Send to fresh subaddress
        - Round 2: Send to another fresh subaddress  
        - Round 3: Final destination
        
        Each round uses different timing and mixin.
        """
        amounts = []
        current = amount
        
        for i in range(rounds):
            # Vary amount slightly each round
            variance = random.uniform(-0.01, 0.01)
            current = current + variance
            amounts.append(current)
            
            # Add delay between churns
            if i < rounds - 1:
                delay = random.randint(3600, 86400)  # 1-24 hours
                time.sleep(delay)  # In real implementation: schedule
        
        return amounts
    
    def select_fingerprint_resistant_fee(
        self,
        base_fee: float
    ) -> float:
        """
        Randomize fee slightly to avoid wallet fingerprinting.
        Different wallets use different fee algorithms.
        """
        variance = random.uniform(-0.0001, 0.0001)
        return max(0, base_fee + variance)


class TransactionScheduler:
    """
    Schedules transactions for optimal privacy timing.
    Prevents time-based correlation analysis.
    """
    
    def __init__(self):
        self._queue: List[dict] = []
        self._last_tx_time: Optional[datetime] = None
        self._min_interval_minutes: int = 5  # Minimum time between txs
    
    def schedule(
        self,
        transaction: dict,
        privacy_level: str = "high"
    ) -> datetime:
        """
        Schedule transaction for future broadcast.
        
        Args:
            transaction: TX data
            privacy_level: "low", "medium", "high", "paranoid"
        
        Returns:
            Scheduled broadcast time
        """
        now = datetime.now()
        
        if privacy_level == "low":
            delay = random.randint(0, 300)  # 0-5 min
        elif privacy_level == "medium":
            delay = random.randint(300, 3600)  # 5-60 min
        elif privacy_level == "high":
            delay = random.randint(3600, 14400)  # 1-4 hours
        elif privacy_level == "paranoid":
            delay = random.randint(14400, 86400)  # 4-24 hours
        else:
            delay = 0
        
        # Ensure minimum interval from last tx
        if self._last_tx_time:
            time_since_last = (now - self._last_tx_time).total_seconds()
            min_interval = self._min_interval_minutes * 60
            if time_since_last < min_interval:
                delay = max(delay, min_interval - time_since_last)
        
        broadcast_time = now + timedelta(seconds=delay)
        
        self._queue.append({
            "transaction": transaction,
            "broadcast_at": broadcast_time,
            "scheduled_at": now
        })
        
        return broadcast_time
    
    def get_pending(self) -> List[dict]:
        """Get transactions ready to broadcast"""
        now = datetime.now()
        ready = []
        pending = []
        
        for item in self._queue:
            if item["broadcast_at"] <= now:
                ready.append(item)
            else:
                pending.append(item)
        
        self._queue = pending
        return ready
    
    def clear_history(self) -> None:
        """Clear scheduling history for privacy"""
        self._queue.clear()
        self._last_tx_time = None


class MetadataStripper:
    """
    Removes metadata that could identify the sender.
    """
    
    @staticmethod
    def strip_user_agent(headers: dict) -> dict:
        """Remove identifying User-Agent"""
        headers.pop("User-Agent", None)
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        return headers
    
    @staticmethod
    def strip_referrer(headers: dict) -> dict:
        """Remove Referrer header"""
        headers.pop("Referer", None)
        headers.pop("Referrer", None)
        return headers
    
    @staticmethod
    def add_noise_to_timing(timestamp: float) -> float:
        """
        Add small random noise to timestamp.
        Prevents precise timing correlation.
        """
        noise = random.uniform(-1.0, 1.0)  # +/- 1 second
        return timestamp + noise
    
    @staticmethod
    def randomize_request_order(requests: List[dict]) -> List[dict]:
        """
        Randomize order of batch requests.
        Prevents sequence-based fingerprinting.
        """
        shuffled = requests.copy()
        random.shuffle(shuffled)
        return shuffled


def calculate_privacy_score(
    mixin: int,
    timing_variance: float,
    address_reuse: bool,
    decoy_outputs: int
) -> int:
    """
    Calculate privacy score 0-100.
    Higher = better privacy.
    """
    score = 0
    
    # Mixin score (max 30)
    score += min(mixin * 2, 30)
    
    # Timing variance (max 25)
    score += min(timing_variance * 100, 25)
    
    # No address reuse (20 points)
    if not address_reuse:
        score += 20
    
    # Decoy outputs (max 25)
    score += min(decoy_outputs * 5, 25)
    
    return min(100, score)
