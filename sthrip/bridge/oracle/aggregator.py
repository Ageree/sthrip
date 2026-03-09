"""
Oracle Aggregator with Outlier Detection

Aggregates prices from multiple sources and removes outliers.
"""

import asyncio
import statistics
from decimal import Decimal
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass
from abc import ABC, abstractmethod

from .chainlink import PriceData


class BaseOracle(ABC):
    """Abstract base class for price oracles"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Oracle name"""
        pass
    
    @abstractmethod
    async def get_price(self, base: str, quote: str) -> Decimal:
        """Get price for base/quote pair"""
        pass
    
    @abstractmethod
    async def health_check(self) -> bool:
        """Check if oracle is healthy"""
        pass


class OracleAggregator:
    """
    Aggregate prices from multiple sources with outlier detection
    
    Features:
    - Multi-source aggregation
    - Statistical outlier removal
    - Weighted averaging by confidence
    - Configurable deviation threshold
    - Async parallel fetching
    
    Example:
        aggregator = OracleAggregator()
        aggregator.add_oracle(ChainlinkOracle())
        aggregator.add_oracle(UniswapV3Oracle())
        
        price = await aggregator.get_price("ETH", "USD")
    """
    
    def __init__(
        self,
        max_deviation: Decimal = Decimal("0.05"),  # 5%
        min_sources: int = 2,
        timeout: float = 10.0
    ):
        """
        Initialize aggregator
        
        Args:
            max_deviation: Maximum allowed deviation from median
            min_sources: Minimum number of sources required
            timeout: Request timeout in seconds
        """
        self.oracles: List[BaseOracle] = []
        self.max_deviation = max_deviation
        self.min_sources = min_sources
        self.timeout = timeout
        
        # Statistics
        self._last_prices: Dict[str, PriceData] = {}
        self._last_update: float = 0
    
    def add_oracle(self, oracle: BaseOracle) -> None:
        """Add an oracle source"""
        self.oracles.append(oracle)
    
    def remove_oracle(self, name: str) -> bool:
        """Remove an oracle by name"""
        for i, oracle in enumerate(self.oracles):
            if oracle.name == name:
                del self.oracles[i]
                return True
        return False
    
    async def get_price(self, base: str, quote: str) -> PriceData:
        """
        Get aggregated price with outlier detection
        
        Args:
            base: Base asset (e.g., "ETH", "XMR")
            quote: Quote asset (e.g., "USD")
            
        Returns:
            PriceData with aggregated price
            
        Raises:
            RuntimeError: If insufficient sources or too many outliers
        """
        # Fetch from all sources in parallel
        tasks = [
            self._fetch_with_timeout(oracle, base, quote)
            for oracle in self.oracles
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Filter successful results
        prices: List[PriceData] = []
        errors = []
        
        for oracle, result in zip(self.oracles, results):
            if isinstance(result, Exception):
                errors.append(f"{oracle.name}: {result}")
            else:
                prices.append(result)
        
        if len(prices) < self.min_sources:
            error_msg = f"Insufficient price sources: {len(prices)}/{self.min_sources}"
            if errors:
                error_msg += f"\nErrors: {errors}"
            raise RuntimeError(error_msg)
        
        # Outlier detection
        valid_prices = self._filter_outliers(prices)
        
        if len(valid_prices) < self.min_sources:
            raise RuntimeError(
                f"Too many outliers after filtering: {len(valid_prices)}/{self.min_sources}"
            )
        
        # Calculate weighted average
        aggregated = self._aggregate_prices(valid_prices)
        
        self._last_prices[f"{base}/{quote}"] = aggregated
        self._last_update = asyncio.get_event_loop().time()
        
        return aggregated
    
    async def _fetch_with_timeout(
        self,
        oracle: BaseOracle,
        base: str,
        quote: str
    ) -> PriceData:
        """Fetch price with timeout"""
        try:
            price = await asyncio.wait_for(
                oracle.get_price(base, quote),
                timeout=self.timeout
            )
            
            return PriceData(
                source=oracle.name,
                price=price,
                timestamp=asyncio.get_event_loop().time(),
                confidence=0.9  # Default confidence
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"Timeout fetching from {oracle.name}")
    
    def _filter_outliers(self, prices: List[PriceData]) -> List[PriceData]:
        """
        Filter outlier prices using statistical methods
        
        1. Calculate median
        2. Remove prices with deviation > max_deviation
        """
        if len(prices) <= 2:
            return prices
        
        # Calculate median
        price_values = [p.price for p in prices]
        median = statistics.median(price_values)
        
        # Filter outliers
        valid = []
        for price_data in prices:
            deviation = abs(price_data.price - median) / median
            if deviation <= self.max_deviation:
                valid.append(price_data)
            else:
                print(f"Outlier detected: {price_data.source} = {price_data.price} "
                      f"(deviation: {deviation:.2%})")
        
        return valid
    
    def _aggregate_prices(self, prices: List[PriceData]) -> PriceData:
        """
        Aggregate prices using weighted average
        
        Weights are based on source confidence.
        """
        if len(prices) == 1:
            return prices[0]
        
        # Calculate weights based on confidence
        total_weight = sum(p.confidence for p in prices)
        weighted_price = sum(
            p.price * p.confidence for p in prices
        ) / total_weight
        
        # Average confidence
        avg_confidence = sum(p.confidence for p in prices) / len(prices)
        
        # Use most recent timestamp
        latest_timestamp = max(p.timestamp for p in prices)
        
        return PriceData(
            source=f"aggregated:{len(prices)}",
            price=weighted_price,
            timestamp=latest_timestamp,
            confidence=avg_confidence
        )
    
    def get_last_price(self, base: str, quote: str) -> Optional[PriceData]:
        """Get last known price (may be stale)"""
        return self._last_prices.get(f"{base}/{quote}")
    
    def get_sources_status(self) -> Dict[str, bool]:
        """Get health status of all sources"""
        # This would need to be async in real implementation
        return {oracle.name: True for oracle in self.oracles}
    
    async def health_check(self) -> bool:
        """Check if aggregator has sufficient healthy sources"""
        healthy = 0
        for oracle in self.oracles:
            try:
                if await oracle.health_check():
                    healthy += 1
            except Exception:
                pass
        
        return healthy >= self.min_sources


class CachedOracleAggregator(OracleAggregator):
    """
    Oracle aggregator with caching support
    
    Caches prices for a specified duration to reduce
    API calls and improve performance.
    """
    
    def __init__(
        self,
        cache_duration: float = 60.0,  # 60 seconds
        **kwargs
    ):
        super().__init__(**kwargs)
        self.cache_duration = cache_duration
        self._cache: Dict[str, tuple] = {}  # key -> (price, timestamp)
    
    async def get_price(self, base: str, quote: str) -> PriceData:
        """Get price with caching"""
        key = f"{base}/{quote}"
        
        # Check cache
        if key in self._cache:
            price, timestamp = self._cache[key]
            age = asyncio.get_event_loop().time() - timestamp
            if age < self.cache_duration:
                return price
        
        # Fetch fresh price
        price = await super().get_price(base, quote)
        self._cache[key] = (price, asyncio.get_event_loop().time())
        
        return price
    
    def invalidate_cache(self, base: str = None, quote: str = None):
        """Invalidate cache entries"""
        if base and quote:
            key = f"{base}/{quote}"
            self._cache.pop(key, None)
        else:
            self._cache.clear()
