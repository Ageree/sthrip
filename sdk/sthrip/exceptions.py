"""Typed exceptions for Sthrip SDK.

All exceptions carry ``status_code`` and ``detail`` so callers can
inspect the failure programmatically without parsing strings.
"""


class StrhipError(Exception):
    """Base exception for every Sthrip SDK error."""

    def __init__(self, detail, status_code=None):
        # type: (str, int) -> None
        super(StrhipError, self).__init__(detail)
        self.detail = detail
        self.status_code = status_code


class AuthError(StrhipError):
    """Raised on 401 Unauthorized or 403 Forbidden."""

    def __init__(self, detail="Authentication failed", status_code=401):
        # type: (str, int) -> None
        super(AuthError, self).__init__(detail, status_code)


class PaymentError(StrhipError):
    """Generic payment failure."""

    def __init__(self, detail="Payment failed", status_code=None):
        # type: (str, int) -> None
        super(PaymentError, self).__init__(detail, status_code)


class InsufficientBalance(PaymentError):
    """Not enough XMR to complete the payment or withdrawal."""

    def __init__(self, detail="Insufficient balance", status_code=None):
        # type: (str, int) -> None
        super(InsufficientBalance, self).__init__(detail, status_code)


class AgentNotFound(PaymentError):
    """Recipient agent does not exist."""

    def __init__(self, detail="Agent not found", status_code=404):
        # type: (str, int) -> None
        super(AgentNotFound, self).__init__(detail, status_code)


class RateLimitError(StrhipError):
    """Too many requests (HTTP 429)."""

    def __init__(self, detail="Rate limit exceeded", status_code=429):
        # type: (str, int) -> None
        super(RateLimitError, self).__init__(detail, status_code)


class NetworkError(StrhipError):
    """Connection or timeout failure talking to the Sthrip API."""

    def __init__(self, detail="Network error", status_code=None):
        # type: (str, int) -> None
        super(NetworkError, self).__init__(detail, status_code)
