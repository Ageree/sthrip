"""
Chainlink Price Feed Integration
"""

import aiohttp
import asyncio
from decimal import Decimal
from typing import Optional, List, Callable
from dataclasses import dataclass


@dataclass
class PriceData:
    """Price data from a source"""
    source: str
    price: Decimal
    timestamp: float
    confidence: float  # 0-1


class ChainlinkOracle:
    """
    Chainlink price feed integration
    
    Supports:
    - ETH/USD on mainnet and testnets
    - Custom price feeds
    """
    
    # Chainlink ETH/USD feeds
    FEEDS = {
        "mainnet": "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419",
        "sepolia": "0x694AA1769357215DE4FAC081bf1f309aDC325306",
        "goerli": "0xD4a33860578De61DBAbDc8BFdb98FD742fA7028e",
    }
    
    # ABI for AggregatorV3Interface
    ABI = '''[
        {"inputs":[],"name":"latestRoundData","outputs":[
            {"internalType":"uint80","name":"roundId","type":"uint80"},
            {"internalType":"int256","name":"answer","type":"int256"},
            {"internalType":"uint256","name":"startedAt","type":"uint256"},
            {"internalType":"uint256","name":"updatedAt","type":"uint256"},
            {"internalType":"uint80","name":"answeredInRound","type":"uint80"}
        ],"stateMutability":"view","type":"function"}
    ]'''
    
    def __init__(self, network: str = "mainnet", rpc_url: str = None):
        """
        Initialize Chainlink oracle
        
        Args:
            network: Network name (mainnet, sepolia, goerli)
            rpc_url: Custom RPC URL (optional)
        """
        self.network = network
        self.feed_address = self.FEEDS.get(network)
        
        if not self.feed_address:
            raise ValueError(f"Unsupported network: {network}")
        
        self.rpc_url = rpc_url or self._get_default_rpc(network)
        self._price_cache: Optional[PriceData] = None
        self._cache_duration = 60  # 60 seconds
    
    def _get_default_rpc(self, network: str) -> str:
        """Get default RPC URL for network"""
        rpcs = {
            "mainnet": "https://eth.llamarpc.com",
            "sepolia": "https://rpc.sepolia.org",
            "goerli": "https://rpc.goerli.mudit.blog",
        }
        return rpcs.get(network, rpcs["mainnet"])
    
    async def get_eth_price(self) -> PriceData:
        """
        Get ETH/USD price from Chainlink
        
        Returns:
            PriceData with price in USD (8 decimals)
        """
        # Check cache
        if self._price_cache and self._is_cache_valid():
            return self._price_cache
        
        # Call Chainlink contract
        price = await self._call_chainlink()
        
        data = PriceData(
            source="chainlink",
            price=price,
            timestamp=asyncio.get_event_loop().time(),
            confidence=0.95  # High confidence for Chainlink
        )
        
        self._price_cache = data
        return data
    
    async def _call_chainlink(self) -> Decimal:
        """Call Chainlink price feed contract"""
        # For async Web3 operations, we'd use web3.py
        # This is a simplified implementation
        
        # Build RPC call
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{
                "to": self.feed_address,
                "data": "0xfeaf968c"  # latestRoundData selector
            }, "latest"],
            "id": 1
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(self.rpc_url, json=payload) as resp:
                result = await resp.json()
                
                if "error" in result:
                    raise RuntimeError(f"RPC error: {result['error']}")
                
                # Parse result
                raw_result = result["result"]
                # Extract answer from ABI-encoded response
                # Skip first 32 bytes (roundId), read next 32 bytes (answer)
                answer_hex = raw_result[66:130]
                answer = int(answer_hex, 16)
                
                # Chainlink returns price with 8 decimals
                if answer >= 2**255:  # Negative (shouldn't happen for prices)
                    answer -= 2**256
                
                return Decimal(answer) / Decimal(10**8)
    
    def _is_cache_valid(self) -> bool:
        """Check if cached price is still valid"""
        if not self._price_cache:
            return False
        
        age = asyncio.get_event_loop().time() - self._price_cache.timestamp
        return age < self._cache_duration
    
    async def get_xmr_price_fallback(self) -> PriceData:
        """
        Get XMR/USD price from multiple sources
        
        Since XMR doesn't have a Chainlink feed, we aggregate
        from multiple centralized exchanges.
        
        Returns:
            PriceData with aggregated price
        """
        sources = [
            self._get_binance_price,
            self._get_kraken_price,
            self._get_coinbase_price,
            self._get_coingecko_price,
        ]
        
        prices = []
        for source in sources:
            try:
                price = await source()
                prices.append(price)
            except Exception as e:
                print(f"Source {source.__name__} failed: {e}")
                continue
        
        if not prices:
            raise RuntimeError("No price sources available")
        
        # Calculate median
        prices.sort(key=lambda x: x.price)
        median = prices[len(prices) // 2]
        
        # Weight by confidence
        total_confidence = sum(p.confidence for p in prices)
        weighted_price = sum(
            p.price * p.confidence for p in prices
        ) / total_confidence
        
        return PriceData(
            source="aggregated",
            price=weighted_price,
            timestamp=asyncio.get_event_loop().time(),
            confidence=median.confidence
        )
    
    async def _get_binance_price(self) -> PriceData:
        """Get XMR price from Binance"""
        url = "https://api.binance.com/api/v3/ticker/price?symbol=XMRUSDT"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                price = Decimal(data["price"])
                
                return PriceData(
                    source="binance",
                    price=price,
                    timestamp=asyncio.get_event_loop().time(),
                    confidence=0.9
                )
    
    async def _get_kraken_price(self) -> PriceData:
        """Get XMR price from Kraken"""
        url = "https://api.kraken.com/0/public/Ticker?pair=XMRUSD"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                price = Decimal(data["result"]["XXMRZUSD"]["c"][0])
                
                return PriceData(
                    source="kraken",
                    price=price,
                    timestamp=asyncio.get_event_loop().time(),
                    confidence=0.9
                )
    
    async def _get_coinbase_price(self) -> PriceData:
        """Get XMR price from Coinbase"""
        url = "https://api.coinbase.com/v2/exchange-rates?currency=XMR"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                price = Decimal(data["data"]["rates"]["USD"])
                
                return PriceData(
                    source="coinbase",
                    price=price,
                    timestamp=asyncio.get_event_loop().time(),
                    confidence=0.85
                )
    
    async def _get_coingecko_price(self) -> PriceData:
        """Get XMR price from CoinGecko"""
        url = "https://api.coingecko.com/api/v3/simple/price?ids=monero&vs_currencies=usd"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                price = Decimal(str(data["monero"]["usd"]))
                
                return PriceData(
                    source="coingecko",
                    price=price,
                    timestamp=asyncio.get_event_loop().time(),
                    confidence=0.8
                )
    
    async def get_xmr_to_eth_rate(self) -> Decimal:
        """
        Calculate XMR/ETH exchange rate
        
        Returns:
            Exchange rate (XMR price in ETH, 18 decimals)
        """
        eth_price = await self.get_eth_price()
        xmr_price = await self.get_xmr_price_fallback()
        
        # rate = XMR/USD / ETH/USD = XMR/ETH
        rate = xmr_price.price / eth_price.price
        
        return rate
    
    async def health_check(self) -> bool:
        """Check if oracle is healthy"""
        try:
            await self.get_eth_price()
            return True
        except Exception:
            return False
