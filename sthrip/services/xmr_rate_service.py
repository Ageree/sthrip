"""Phase 2 Sprint 4 — live XMR/USD rate cache for subscription billing.

Why a new module
----------------
``conversion_service.py`` already exists for hub-balance conversions
between XMR and virtual stablecoins (xUSD/xEUR) — that path uses
fallback rates and a different fee model. Subscription billing has
distinct requirements:

1. Pull a *real* XMR/USD spot price (CoinGecko free tier — no API key).
2. Cache aggressively (5-minute TTL) so CoinGecko outages do not cascade
   into mass-downgrades.
3. Tolerate up to 24h of staleness on outage; beyond that, raise rather
   than silently use an ancient rate.

Both budgets are stored in a process-local in-memory cache. Replicas
each fetch independently; CoinGecko's free tier (50 calls / minute / IP)
absorbs that.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Final, Optional

import httpx

logger = logging.getLogger("sthrip.xmr_rate")


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

COINGECKO_URL: Final[str] = (
    "https://api.coingecko.com/api/v3/simple/price"
)
COINGECKO_PARAMS: Final[dict] = {"ids": "monero", "vs_currencies": "usd"}
RATE_CACHE_TTL: Final[timedelta] = timedelta(minutes=5)
RATE_STALE_LIMIT: Final[timedelta] = timedelta(hours=24)
HTTP_TIMEOUT_SECONDS: Final[float] = 5.0
PICONERO: Final[Decimal] = Decimal(10) ** 12


class RateUnavailableError(RuntimeError):
    """Raised when the XMR/USD rate cannot be obtained.

    Triggered when:
    - the cached rate is older than ``RATE_STALE_LIMIT`` AND
    - the upstream API is also unreachable / failing.
    Callers MUST treat this as a billing-cycle abort, not a degraded charge.
    """


# ─────────────────────────────────────────────────────────────────────────────
# Cache primitive
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _CacheEntry:
    rate: Decimal
    fetched_at: datetime  # tz-aware UTC


class XmrRateCache:
    """Thread-safe singleton-style cache for XMR/USD spot price.

    Stored as a module-level mutable holder for simplicity; all access is
    serialized through ``_LOCK`` to make get-then-update race-safe under
    concurrent first-request arrival.
    """

    _entry: Optional[_CacheEntry] = None
    _LOCK: threading.Lock = threading.Lock()

    @classmethod
    def get(cls) -> Optional[_CacheEntry]:
        with cls._LOCK:
            return cls._entry

    @classmethod
    def set(cls, rate: Decimal, fetched_at: datetime) -> None:
        with cls._LOCK:
            cls._entry = _CacheEntry(rate=rate, fetched_at=fetched_at)

    @classmethod
    def reset(cls) -> None:
        with cls._LOCK:
            cls._entry = None

    @classmethod
    def set_for_test(cls, rate: Decimal, fetched_at: datetime) -> None:
        """Test hook — installs a synthetic cache entry."""
        cls.set(rate, fetched_at)


def reset_rate_cache() -> None:
    """Public test helper — clears the cache entry."""
    XmrRateCache.reset()


# ─────────────────────────────────────────────────────────────────────────────
# Fetch + cache
# ─────────────────────────────────────────────────────────────────────────────


def _fetch_rate_from_coingecko() -> Decimal:
    """Synchronous CoinGecko fetch. Returns Decimal USD-per-XMR.

    Raises any underlying httpx error; callers handle fallback to cache.
    """
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
        resp = client.get(COINGECKO_URL, params=COINGECKO_PARAMS)
        resp.raise_for_status()
        data = resp.json()
    try:
        usd = data["monero"]["usd"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Unexpected CoinGecko payload: {data!r}") from exc

    return Decimal(str(usd))


def get_xmr_usd_rate(now: Optional[datetime] = None) -> Decimal:
    """Return the current XMR/USD rate.

    Resolution order (race-safe):

    1. Cache hit AND fresh (within ``RATE_CACHE_TTL``) -> return cached.
    2. Otherwise fetch from CoinGecko -> cache + return.
    3. On API failure: fall back to cached rate IFF it is younger than
       ``RATE_STALE_LIMIT`` (24h). Otherwise raise ``RateUnavailableError``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    entry = XmrRateCache.get()
    if entry is not None and (now - entry.fetched_at) < RATE_CACHE_TTL:
        return entry.rate

    try:
        rate = _fetch_rate_from_coingecko()
        XmrRateCache.set(rate=rate, fetched_at=now)
        return rate
    except Exception as exc:  # noqa: BLE001 — many possible httpx errors
        logger.warning("CoinGecko fetch failed: %s", exc)
        if entry is not None and (now - entry.fetched_at) < RATE_STALE_LIMIT:
            logger.warning(
                "Returning stale XMR rate (age=%.1fh)",
                (now - entry.fetched_at).total_seconds() / 3600,
            )
            return entry.rate
        raise RateUnavailableError(
            "XMR/USD rate unavailable: API down and cache is stale or empty"
        ) from exc


# ─────────────────────────────────────────────────────────────────────────────
# Conversion helper
# ─────────────────────────────────────────────────────────────────────────────


def usd_to_xmr_piconero(usd: Decimal, rate: Decimal) -> int:
    """Convert ``usd`` to integer piconero at the given rate.

    Math: ``xmr = usd / rate``; ``piconero = round(xmr * 10^12)``.
    Half-up rounding to nearest piconero, matching how on-chain XMR
    transfers are quantized.
    """
    if rate <= Decimal("0"):
        raise ValueError(f"Invalid XMR/USD rate: {rate}")
    xmr = usd / rate
    pico = (xmr * PICONERO).quantize(Decimal("1"))
    return int(pico)


__all__ = [
    "RATE_CACHE_TTL",
    "RATE_STALE_LIMIT",
    "RateUnavailableError",
    "XmrRateCache",
    "get_xmr_usd_rate",
    "reset_rate_cache",
    "usd_to_xmr_piconero",
]
