"""Tests for get_client_ip() helper — TDD RED phase.

Covers:
- Normal request with client.host set
- Request where client is None (proxy, test runner, etc.)
- Request where client.host is an empty string
- Request object is None (defensive: deps.py passes request=None guard)
- IPv6 address passthrough
- IPv4 address passthrough
"""

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helper import
# ---------------------------------------------------------------------------

def _import_helper():
    """Import get_client_ip lazily so tests fail descriptively when missing."""
    from api.helpers import get_client_ip  # noqa: PLC0415
    return get_client_ip


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_request(host=None, client_present=True):
    """Build a mock Starlette Request with controllable .client attribute."""
    req = MagicMock()
    if client_present and host is not None:
        req.client = MagicMock()
        req.client.host = host
    elif client_present and host is None:
        # client object exists but host is None/missing — unlikely in practice
        req.client = MagicMock()
        req.client.host = None
    else:
        req.client = None
    return req


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetClientIp:
    """Unit tests for api.helpers.get_client_ip."""

    def test_returns_host_when_client_is_set(self):
        """Returns request.client.host when the client connection is present."""
        get_client_ip = _import_helper()
        request = _make_request(host="192.168.1.100")
        assert get_client_ip(request) == "192.168.1.100"

    def test_returns_unknown_when_client_is_none(self):
        """Returns 'unknown' when request.client is None (e.g. test runner, proxy)."""
        get_client_ip = _import_helper()
        request = _make_request(client_present=False)
        assert get_client_ip(request) == "unknown"

    def test_returns_unknown_when_request_itself_is_none(self):
        """Returns 'unknown' when the request object is None (guard used in deps.py)."""
        get_client_ip = _import_helper()
        assert get_client_ip(None) == "unknown"

    def test_returns_ipv4_address(self):
        """Passes through standard IPv4 addresses unchanged."""
        get_client_ip = _import_helper()
        request = _make_request(host="10.0.0.1")
        assert get_client_ip(request) == "10.0.0.1"

    def test_returns_ipv6_address(self):
        """Passes through IPv6 addresses unchanged."""
        get_client_ip = _import_helper()
        request = _make_request(host="::1")
        assert get_client_ip(request) == "::1"

    def test_returns_localhost(self):
        """Passes through 127.0.0.1 (common in tests)."""
        get_client_ip = _import_helper()
        request = _make_request(host="127.0.0.1")
        assert get_client_ip(request) == "127.0.0.1"

    def test_returns_unknown_when_client_host_is_none(self):
        """Returns 'unknown' when client exists but .host is None."""
        get_client_ip = _import_helper()
        request = _make_request(host=None, client_present=True)
        assert get_client_ip(request) == "unknown"

    def test_returns_unknown_when_client_host_is_empty_string(self):
        """Returns 'unknown' when client.host is an empty string."""
        get_client_ip = _import_helper()
        request = _make_request(host="", client_present=True)
        assert get_client_ip(request) == "unknown"

    def test_return_type_is_always_str(self):
        """Return value must always be str, never None."""
        get_client_ip = _import_helper()
        for host, present in [("1.2.3.4", True), (None, False), (None, True)]:
            result = get_client_ip(_make_request(host=host, client_present=present))
            assert isinstance(result, str), f"Expected str, got {type(result)} for host={host!r}"

    def test_does_not_mutate_request(self):
        """get_client_ip must not modify the request object (immutability rule)."""
        get_client_ip = _import_helper()
        request = _make_request(host="1.2.3.4")
        original_client = request.client
        get_client_ip(request)
        assert request.client is original_client
