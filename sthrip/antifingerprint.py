"""
Anti-fingerprinting measures to prevent wallet identification.
Different wallets have different behaviors - we randomize everything.

All randomness here is privacy-critical. We use the ``secrets`` module so that
an observer cannot recover the PRNG state from a handful of outputs (Mersenne
Twister is unsuitable for adversarial-privacy decisions).
"""

import secrets
import time
from typing import Optional, List, Dict
from enum import Enum
from dataclasses import dataclass


class _SecureRandom:
    """Cryptographically secure drop-in replacements for the ``random`` API used here."""

    @staticmethod
    def randint(low: int, high: int) -> int:
        if high < low:
            raise ValueError("high must be >= low")
        return low + secrets.randbelow(high - low + 1)

    @staticmethod
    def uniform(low: float, high: float) -> float:
        if high < low:
            low, high = high, low
        rand_int = int.from_bytes(secrets.token_bytes(7), "big") >> 3
        frac = rand_int / (1 << 53)
        return low + (high - low) * frac

    @staticmethod
    def random() -> float:
        rand_int = int.from_bytes(secrets.token_bytes(7), "big") >> 3
        return rand_int / (1 << 53)

    @staticmethod
    def choice(seq):
        if not seq:
            raise IndexError("Cannot choose from empty sequence")
        return seq[secrets.randbelow(len(seq))]


random = _SecureRandom()  # type: ignore[assignment]


class WalletType(Enum):
    """Different wallet types have different behaviors"""
    OFFICIAL = "official"           # GUI/CLI wallet
    CAKE = "cake"                   # Cake Wallet
    MONERUJO = "monerujo"          # Android
    FEATHER = "feather"             # Feather Wallet
    MYMONERO = "mymonero"          # MyMonero
    EDGE = "edge"                   # Edge Wallet


@dataclass
class FingerprintProfile:
    """Behavior profile to mimic"""
    wallet_type: WalletType
    fee_level: str  # "low", "medium", "high", "auto"
    ring_size_range: tuple  # (min, max)
    timing_variance: float  # seconds
    output_count: range     # Number of outputs preferred
    decoy_probability: float  # Chance of decoy tx


class FingerprintRandomizer:
    """
    Randomizes transaction behavior to mimic different wallets.
    Prevents blockchain analysis from identifying Sthrip users.
    """
    
    # Profiles of real wallets
    PROFILES: Dict[WalletType, FingerprintProfile] = {
        WalletType.OFFICIAL: FingerprintProfile(
            wallet_type=WalletType.OFFICIAL,
            fee_level="auto",
            ring_size_range=(11, 16),
            timing_variance=5.0,
            output_count=range(2, 4),
            decoy_probability=0.05
        ),
        WalletType.CAKE: FingerprintProfile(
            wallet_type=WalletType.CAKE,
            fee_level="medium",
            ring_size_range=(11, 11),  # Fixed at 11
            timing_variance=2.0,
            output_count=range(2, 3),
            decoy_probability=0.02
        ),
        WalletType.FEATHER: FingerprintProfile(
            wallet_type=WalletType.FEATHER,
            fee_level="low",
            ring_size_range=(11, 20),
            timing_variance=10.0,
            output_count=range(2, 5),
            decoy_probability=0.08
        ),
    }
    
    def __init__(self):
        self.current_profile: Optional[FingerprintProfile] = None
        self.transaction_count = 0
        self._switch_threshold = random.randint(5, 15)  # Switch profile every N tx
    
    def get_profile(self) -> FingerprintProfile:
        """Get current behavior profile (switch periodically)"""
        if (self.current_profile is None or 
            self.transaction_count >= self._switch_threshold):
            self._switch_profile()
        
        self.transaction_count += 1
        return self.current_profile
    
    def _switch_profile(self) -> None:
        """Switch to random wallet profile"""
        self.current_profile = random.choice(list(self.PROFILES.values()))
        self.transaction_count = 0
        self._switch_threshold = random.randint(5, 15)
    
    def get_ring_size(self) -> int:
        """Get ring size according to current profile"""
        profile = self.get_profile()
        return random.randint(*profile.ring_size_range)
    
    def get_fee_multiplier(self) -> float:
        """Get fee preference multiplier"""
        profile = self.get_profile()
        
        multipliers = {
            "low": random.uniform(1.0, 1.5),
            "medium": random.uniform(1.5, 2.5),
            "high": random.uniform(2.5, 5.0),
            "auto": random.uniform(1.0, 3.0)
        }
        
        return multipliers.get(profile.fee_level, 1.0)
    
    def should_add_decoy(self) -> bool:
        """Decide if we should add a decoy transaction"""
        profile = self.get_profile()
        return random.random() < profile.decoy_probability
    
    def get_output_count(self) -> int:
        """Get preferred number of outputs"""
        profile = self.get_profile()
        return random.choice(list(profile.output_count))
    
    def get_delay(self) -> float:
        """Get random delay according to profile"""
        profile = self.get_profile()
        return random.uniform(0, profile.timing_variance)


class AmountRandomizer:
    """
    Randomize payment amounts to avoid round number detection.
    e.g., instead of exactly 1.0 XMR, send 0.99987342
    """
    
    @staticmethod
    def fuzz(amount: float, precision: int = 8) -> float:
        """
        Add random fuzz to amount.
        
        Args:
            amount: Base amount
            precision: Decimal places (Monero = 12, but 8 is enough for fuzz)
        
        Returns:
            Fuzzed amount
        """
        # Generate random small variance
        variance = random.uniform(-0.01, 0.01)  # +/- 1%
        fuzzed = amount * (1 + variance)
        
        # Round to random precision (avoid always same precision)
        decimals = random.randint(6, 12)
        return round(fuzzed, decimals)
    
    @staticmethod
    def round_to_denomination(amount: float) -> float:
        """
        Round to standard denomination (like real users do).
        Sometimes round, sometimes precise.
        """
        if random.random() < 0.3:
            # 30% chance - round number
            return round(amount, random.randint(1, 3))
        else:
            # 70% chance - random precision
            return round(amount, random.randint(4, 8))


class TimingRandomizer:
    """
    Randomize transaction timing to avoid pattern detection.
    """
    
    PATTERNS = [
        "immediate",      # Send immediately
        "bursty",         # Group transactions
        "regular",        # Regular intervals
        "random",         # Completely random
        "business_hours"  # Only business hours
    ]
    
    def __init__(self):
        self.pattern = random.choice(self.PATTERNS)
        self.last_tx_time: Optional[float] = None
    
    def get_wait_time(self) -> float:
        """Get time to wait before next transaction"""
        if self.pattern == "immediate":
            return 0
        
        elif self.pattern == "bursty":
            # Short gap within burst, long gap between bursts
            if self.last_tx_time and (time.time() - self.last_tx_time) < 60:
                return random.uniform(0, 5)  # Within burst
            else:
                return random.uniform(300, 3600)  # Between bursts
        
        elif self.pattern == "regular":
            return random.uniform(900, 1800)  # 15-30 min
        
        elif self.pattern == "random":
            return random.uniform(0, 7200)  # 0-2 hours
        
        elif self.pattern == "business_hours":
            # Calculate delay until business hours
            from datetime import datetime
            now = datetime.now()
            if 9 <= now.hour < 17:
                return random.uniform(0, 300)
            else:
                # Wait until 9 AM
                target = now.replace(hour=9, minute=0, second=0)
                if target < now:
                    target = target.replace(day=target.day + 1)
                return (target - now).total_seconds()
        
        return 0
    
    def record_transaction(self) -> None:
        """Record that we made a transaction"""
        self.last_tx_time = time.time()


class DecoyManager:
    """
    Manage decoy (fake) transactions to confuse analysis.
    """
    
    def __init__(self, decoy_probability: float = 0.1):
        self.decoy_probability = decoy_probability
        self.decoy_amounts = [0.001, 0.01, 0.1, 0.5, 1.0]
    
    def maybe_create_decoy(self) -> Optional[float]:
        """
        Decide if we should create a decoy transaction.
        Returns amount if yes, None if no.
        """
        if random.random() < self.decoy_probability:
            return random.choice(self.decoy_amounts)
        return None
    
    def create_decoy_address(self) -> str:
        """Generate random-looking decoy address"""
        # In production: generate real Monero address
        import hashlib
        import time
        random_data = f"{time.time()}{random.random()}"
        fake_hash = hashlib.sha256(random_data.encode()).hexdigest()
        return f"4{'decoy'.ljust(93, '0')}{fake_hash[:2]}"


class FingerprintChecker:
    """
    Check if transactions are fingerprintable.
    """
    
    @staticmethod
    def analyze_transaction_pattern(transactions: List[dict]) -> dict:
        """
        Analyze list of transactions for fingerprinting risks.
        
        Returns:
            Dict with risk scores
        """
        if not transactions:
            return {"risk": "unknown", "score": 0}
        
        risks = []
        
        # Check timing regularity
        if len(transactions) > 2:
            intervals = []
            for i in range(1, len(transactions)):
                interval = transactions[i]["time"] - transactions[i-1]["time"]
                intervals.append(interval)
            
            if intervals:
                avg_interval = sum(intervals) / len(intervals)
                variance = sum((x - avg_interval) ** 2 for x in intervals) / len(intervals)
                std_dev = variance ** 0.5
                
                if std_dev < 60:  # Less than 1 minute variance
                    risks.append("Very regular timing - fingerprintable!")
        
        # Check amount patterns
        amounts = [tx["amount"] for tx in transactions]
        unique_amounts = len(set(amounts))
        if unique_amounts < len(amounts) / 2:
            risks.append("Repeated amounts - fingerprintable!")
        
        # Check ring sizes
        ring_sizes = [tx.get("ring_size", 11) for tx in transactions]
        if len(set(ring_sizes)) == 1:
            risks.append("Fixed ring size - fingerprintable!")
        
        return {
            "risk": "high" if len(risks) > 1 else "medium" if risks else "low",
            "issues": risks,
            "transaction_count": len(transactions)
        }
