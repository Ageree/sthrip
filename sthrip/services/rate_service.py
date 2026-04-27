"""
RateService — exchange rate provider for cross-chain swaps.

Rates are fetched from CoinGecko and cached in-memory for 60 seconds.
A hardcoded fallback is used when the API is unavailable (e.g. in tests).
"""

import logging
import time
from decimal import Decimal
from typing import Dict

logger = logging.getLogger("sthrip.rate_service")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_PAIRS: frozenset = frozenset(
    {"BTC_XMR", "XMR_BTC", "ETH_XMR", "SOL_XMR", "XMR_USD", "XMR_EUR"}
)

_CACHE_TTL_SECONDS: int = 60
_FEE_PERCENT: Decimal = Decimal("0.01")  # 1% fee on all swaps

# Hardcoded fallback rates (used when CoinGecko is unreachable).
_FALLBACK_RATES: Dict[str, Decimal] = {
    "BTC_XMR": Decimal("150.0"),
    "XMR_BTC": Decimal("0.006667"),
    "ETH_XMR": Decimal("10.0"),
    "SOL_XMR": Decimal("0.234"),
    "XMR_USD": Decimal("180.0"),
    "XMR_EUR": Decimal("160.0"),
}

# ---------------------------------------------------------------------------
# Module-level cache (shared across RateService instances in a process)
# ---------------------------------------------------------------------------

_rate_cache: Dict[str, Decimal] = {}
_cache_timestamps: Dict[str, float] = {}


class RateService:
    """Provides exchange rates for supported cross-chain swap pairs."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_rate(self, from_currency: str, to_currency: str) -> Decimal:
        """Return the exchange rate for from_currency → to_currency.

        Reads from in-memory cache; refreshes if the entry is older than
        _CACHE_TTL_SECONDS.

        Raises:
            ValueError: if the pair is not in SUPPORTED_PAIRS.
        """
        pair = f"{from_currency}_{to_currency}"
        if pair not in SUPPORTED_PAIRS:
            raise ValueError(
                f"Unsupported pair: {pair}. Supported: {sorted(SUPPORTED_PAIRS)}"
            )

        now = time.monotonic()
        if pair not in _rate_cache or (now - _cache_timestamps.get(pair, 0.0)) >= _CACHE_TTL_SECONDS:
            self._refresh_cache()

        return _rate_cache.get(pair, _FALLBACK_RATES[pair])

    def get_rates(self) -> Dict[str, Decimal]:
        """Return all supported pair rates as a dict.

        Always returns a fresh copy so callers cannot mutate the cache.
        """
        self._refresh_cache()
        return dict(_rate_cache)

    def get_quote(
        self,
        from_currency: str,
        from_amount: Decimal,
        to_currency: str = "XMR",
    ) -> dict:
        """Compute a swap quote.

        Returns:
            {
                "from_currency": str,
                "to_currency": str,
                "from_amount": str,
                "rate": str,
                "fee": str,          # 1% of from_amount
                "to_amount": str,    # (from_amount - fee) * rate
                "expires_in": int,   # seconds until quote expires (300)
            }

        Raises:
            ValueError: if from_amount <= 0 or pair unsupported.
        """
        if from_amount <= Decimal("0"):
            raise ValueError("from_amount must be greater than zero")

        rate = self.get_rate(from_currency, to_currency)
        fee = (from_amount * _FEE_PERCENT).quantize(Decimal("0.00000001"))
        net_amount = from_amount - fee
        to_amount = (net_amount * rate).quantize(Decimal("0.00000001"))

        return {
            "from_currency": from_currency,
            "to_currency": to_currency,
            "from_amount": str(from_amount),
            "rate": str(rate),
            "fee": str(fee),
            "to_amount": str(to_amount),
            "expires_in": 300,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_rates(self) -> Dict[str, Decimal]:
        """Fetch live rates from CoinGecko.

        Returns a dict of pair → Decimal rate on success.
        Falls back to _FALLBACK_RATES on any exception.

        This method makes an actual HTTP call — mock it in tests.
        """
        try:
            import urllib.request
            import json

            url = (
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin,ethereum,solana,monero"
                "&vs_currencies=xmr,btc,usd,eur"
            )
            # B310 false positive: URL is a hardcoded https://api.coingecko.com literal,
            # not user-influenced; SSRF/file:// schemes not reachable here.
            with urllib.request.urlopen(url, timeout=5) as resp:  # nosec B310
                data = json.loads(resp.read().decode())

            rates: Dict[str, Decimal] = {}

            btc_xmr = data.get("bitcoin", {}).get("xmr")
            if btc_xmr:
                rates["BTC_XMR"] = Decimal(str(btc_xmr))
                if btc_xmr != 0:
                    rates["XMR_BTC"] = Decimal(str(round(1.0 / float(btc_xmr), 8)))

            eth_xmr = data.get("ethereum", {}).get("xmr")
            if eth_xmr:
                rates["ETH_XMR"] = Decimal(str(eth_xmr))

            sol_xmr = data.get("solana", {}).get("xmr")
            if sol_xmr:
                rates["SOL_XMR"] = Decimal(str(sol_xmr))

            xmr_usd = data.get("monero", {}).get("usd")
            if xmr_usd:
                rates["XMR_USD"] = Decimal(str(xmr_usd))

            xmr_eur = data.get("monero", {}).get("eur")
            if xmr_eur:
                rates["XMR_EUR"] = Decimal(str(xmr_eur))

            # Fill any missing pairs with fallback
            for pair, fallback in _FALLBACK_RATES.items():
                rates.setdefault(pair, fallback)

            return rates
        except Exception as exc:
            logger.warning("RateService: CoinGecko unavailable, using fallback: %s", exc)
            return dict(_FALLBACK_RATES)

    def _refresh_cache(self) -> None:
        """Refresh the module-level cache if TTL has expired (or cache is empty)."""
        now = time.monotonic()
        # Use BTC_XMR as the representative key for cache-wide TTL check.
        sentinel = "BTC_XMR"
        if _rate_cache and (now - _cache_timestamps.get(sentinel, 0.0)) < _CACHE_TTL_SECONDS:
            return

        fresh = self._fetch_rates()
        timestamp = time.monotonic()
        for pair, rate in fresh.items():
            _rate_cache[pair] = rate
            _cache_timestamps[pair] = timestamp
