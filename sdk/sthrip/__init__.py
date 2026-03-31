"""Sthrip SDK -- anonymous payments for AI agents.

Quick start::

    from sthrip import Sthrip

    s = Sthrip()                       # auto-registers if no key found
    print(s.deposit_address())         # get XMR deposit address
    print(s.balance())                 # check balance
    s.pay("other-agent", 0.05)         # send payment
"""

from .client import Sthrip
from .exceptions import (
    AgentNotFound,
    AuthError,
    InsufficientBalance,
    NetworkError,
    PaymentError,
    RateLimitError,
    StrhipError,
)

__version__ = "0.3.0"

__all__ = [
    "Sthrip",
    "StrhipError",
    "AuthError",
    "PaymentError",
    "InsufficientBalance",
    "AgentNotFound",
    "RateLimitError",
    "NetworkError",
]
