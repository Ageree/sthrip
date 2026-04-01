"""Tests for SDK multi-currency methods: swap_rates, swap_quote, swap, convert, balances_all.

TDD: tests written first before implementation.
All HTTP calls are intercepted via unittest.mock -- no real network calls.
"""

import sys
import os
import importlib.util
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch

# The SDK's Sthrip client lives under sdk/sthrip/, distinct from the
# sthrip/ package at the project root.  Load it under a unique module name
# ("sthrip_sdk") so it never conflicts with the project-level sthrip package
# that conftest.py and all application code depend on.
_SDK_CLIENT_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "sdk", "sthrip", "client.py")
)
_SDK_PKG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "sdk")
)


def _load_sdk_sthrip():
    """Load sdk/sthrip/client.Sthrip without polluting the main sys.modules."""
    # Register the sdk sthrip package under a unique namespace so relative
    # imports inside the SDK resolve correctly.
    _sdk_pkg_name = "sthrip_sdk_pkg"
    _sdk_client_name = "sthrip_sdk_pkg.client"

    if _sdk_pkg_name not in sys.modules:
        # Create a minimal package entry for the sdk sthrip package
        pkg_init = os.path.join(_SDK_PKG_DIR, "sthrip", "__init__.py")
        pkg_spec = importlib.util.spec_from_file_location(
            _sdk_pkg_name,
            pkg_init,
            submodule_search_locations=[os.path.dirname(pkg_init)],
        )
        pkg_mod = importlib.util.module_from_spec(pkg_spec)
        sys.modules[_sdk_pkg_name] = pkg_mod
        pkg_spec.loader.exec_module(pkg_mod)

    if _sdk_client_name not in sys.modules:
        client_spec = importlib.util.spec_from_file_location(
            _sdk_client_name,
            _SDK_CLIENT_FILE,
        )
        client_mod = importlib.util.module_from_spec(client_spec)
        client_mod.__package__ = _sdk_pkg_name
        sys.modules[_sdk_client_name] = client_mod
        # Pre-register sibling modules so relative imports work
        for sibling in ("auth", "exceptions"):
            sib_name = f"{_sdk_pkg_name}.{sibling}"
            if sib_name not in sys.modules:
                sib_file = os.path.join(_SDK_PKG_DIR, "sthrip", f"{sibling}.py")
                if os.path.exists(sib_file):
                    sib_spec = importlib.util.spec_from_file_location(sib_name, sib_file)
                    sib_mod = importlib.util.module_from_spec(sib_spec)
                    sib_mod.__package__ = _sdk_pkg_name
                    sys.modules[sib_name] = sib_mod
                    sib_spec.loader.exec_module(sib_mod)
        client_spec.loader.exec_module(client_mod)

    return sys.modules[_sdk_client_name].Sthrip


_SDKSthrip = _load_sdk_sthrip()


# ---------------------------------------------------------------------------
# Fixture: SDK client with mocked HTTP session
# ---------------------------------------------------------------------------

@pytest.fixture
def sdk_client(monkeypatch):
    """Return a Sthrip (SDK) client whose HTTP calls are fully mocked."""
    monkeypatch.setenv("STHRIP_API_KEY", "test-api-key-sdk")
    monkeypatch.setenv("STHRIP_API_URL", "http://test-host")

    client = _SDKSthrip.__new__(_SDKSthrip)
    client._api_url = "http://test-host"
    client._api_key = "test-api-key-sdk"
    client._session = MagicMock()
    client._session_id = "test-session-id"
    client._session_spent = Decimal("0")
    client._max_per_session = None
    client._max_per_tx = None
    client._daily_limit = None
    client._allowed_agents = None
    client._require_escrow_above = None

    return client


# ---------------------------------------------------------------------------
# Tests: swap_rates
# ---------------------------------------------------------------------------

class TestSwapRates:
    def test_swap_rates_calls_correct_endpoint(self, sdk_client):
        """swap_rates() calls GET /v2/swap/rates."""
        expected = {"XMR_USD": "150.00", "XMR_EUR": "138.00"}
        sdk_client._raw_get = MagicMock(return_value=expected)

        result = sdk_client.swap_rates()

        sdk_client._raw_get.assert_called_once_with("/v2/swap/rates")
        assert result == expected

    def test_swap_rates_returns_response(self, sdk_client):
        """swap_rates() returns the raw API response unchanged."""
        rates = {"XMR_USD": "155.0"}
        sdk_client._raw_get = MagicMock(return_value=rates)

        result = sdk_client.swap_rates()
        assert result is rates


# ---------------------------------------------------------------------------
# Tests: swap_quote
# ---------------------------------------------------------------------------

class TestSwapQuote:
    def test_swap_quote_default_to_currency(self, sdk_client):
        """swap_quote() defaults to_currency to 'XMR'."""
        expected = {"rate": "150.0", "to_amount": "1.0"}
        sdk_client._raw_post = MagicMock(return_value=expected)

        result = sdk_client.swap_quote(from_currency="xUSD", from_amount=Decimal("150.0"))

        sdk_client._raw_post.assert_called_once_with(
            "/v2/swap/quote",
            json_body={
                "from_currency": "xUSD",
                "from_amount": "150.0",
                "to_currency": "XMR",
            },
        )
        assert result == expected

    def test_swap_quote_explicit_to_currency(self, sdk_client):
        """swap_quote() forwards explicit to_currency."""
        sdk_client._raw_post = MagicMock(return_value={})

        sdk_client.swap_quote(from_currency="XMR", from_amount="1.0", to_currency="xEUR")

        _, kwargs = sdk_client._raw_post.call_args
        assert kwargs["json_body"]["to_currency"] == "xEUR"

    def test_swap_quote_converts_amount_to_string(self, sdk_client):
        """swap_quote() converts numeric from_amount to string."""
        sdk_client._raw_post = MagicMock(return_value={})

        sdk_client.swap_quote(from_currency="XMR", from_amount=2.5)

        _, kwargs = sdk_client._raw_post.call_args
        assert kwargs["json_body"]["from_amount"] == "2.5"


# ---------------------------------------------------------------------------
# Tests: swap
# ---------------------------------------------------------------------------

class TestSwap:
    def test_swap_calls_correct_endpoint(self, sdk_client):
        """swap() calls POST /v2/swap/create."""
        expected = {"swap_id": "abc", "status": "pending"}
        sdk_client._raw_post = MagicMock(return_value=expected)

        result = sdk_client.swap(from_currency="xUSD", from_amount=Decimal("150.0"))

        sdk_client._raw_post.assert_called_once_with(
            "/v2/swap/create",
            json_body={"from_currency": "xUSD", "from_amount": "150.0"},
        )
        assert result == expected

    def test_swap_converts_amount_to_string(self, sdk_client):
        """swap() converts numeric from_amount to string."""
        sdk_client._raw_post = MagicMock(return_value={})

        sdk_client.swap(from_currency="XMR", from_amount=3)

        _, kwargs = sdk_client._raw_post.call_args
        assert kwargs["json_body"]["from_amount"] == "3"

    def test_swap_returns_response(self, sdk_client):
        """swap() returns the raw API response."""
        resp = {"swap_id": "xyz"}
        sdk_client._raw_post = MagicMock(return_value=resp)

        result = sdk_client.swap(from_currency="xUSD", from_amount=1)
        assert result is resp


# ---------------------------------------------------------------------------
# Tests: convert
# ---------------------------------------------------------------------------

class TestConvert:
    def test_convert_calls_correct_endpoint(self, sdk_client):
        """convert() calls POST /v2/balance/convert."""
        expected = {
            "from_currency": "XMR",
            "from_amount": "1.0",
            "to_currency": "xUSD",
            "to_amount": "149.25",
            "rate": "150.0",
            "fee_amount": "0.75",
        }
        sdk_client._raw_post = MagicMock(return_value=expected)

        result = sdk_client.convert(
            from_currency="XMR",
            to_currency="xUSD",
            amount=Decimal("1.0"),
        )

        sdk_client._raw_post.assert_called_once_with(
            "/v2/balance/convert",
            json_body={
                "from_currency": "XMR",
                "to_currency": "xUSD",
                "amount": "1.0",
            },
        )
        assert result == expected

    def test_convert_converts_amount_to_string(self, sdk_client):
        """convert() converts numeric amount to string."""
        sdk_client._raw_post = MagicMock(return_value={})

        sdk_client.convert(from_currency="xUSD", to_currency="XMR", amount=99.5)

        _, kwargs = sdk_client._raw_post.call_args
        assert kwargs["json_body"]["amount"] == "99.5"

    def test_convert_returns_response(self, sdk_client):
        """convert() returns the API response unchanged."""
        resp = {"from_currency": "XMR", "to_currency": "xUSD"}
        sdk_client._raw_post = MagicMock(return_value=resp)

        result = sdk_client.convert("XMR", "xUSD", "1.0")
        assert result is resp

    def test_convert_zero_amount_passes_through(self, sdk_client):
        """convert() passes zero amount as string (server validates)."""
        sdk_client._raw_post = MagicMock(return_value={})

        sdk_client.convert("XMR", "xUSD", 0)

        _, kwargs = sdk_client._raw_post.call_args
        assert kwargs["json_body"]["amount"] == "0"


# ---------------------------------------------------------------------------
# Tests: balances_all
# ---------------------------------------------------------------------------

class TestBalancesAll:
    def test_balances_all_calls_correct_endpoint(self, sdk_client):
        """balances_all() calls GET /v2/balance/all."""
        expected = {"balances": {"XMR": "5.0", "xUSD": "100.0"}}
        sdk_client._raw_get = MagicMock(return_value=expected)

        result = sdk_client.balances_all()

        sdk_client._raw_get.assert_called_once_with("/v2/balance/all")
        assert result == expected

    def test_balances_all_returns_response(self, sdk_client):
        """balances_all() returns the raw response unchanged."""
        resp = {"balances": {}}
        sdk_client._raw_get = MagicMock(return_value=resp)

        result = sdk_client.balances_all()
        assert result is resp
