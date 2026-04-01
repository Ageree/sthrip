"""Unit tests for Sthrip SDK payment scaling methods (Phase 3b).

Tests cover:
- channel_open()   -- POST /v2/channels
- channel_settle() -- POST /v2/channels/{id}/settle
- channel_close()  -- POST /v2/channels/{id}/close
- channels()       -- GET  /v2/channels
- subscribe()      -- POST /v2/subscriptions
- unsubscribe()    -- DELETE /v2/subscriptions/{id}
- subscriptions()  -- GET  /v2/subscriptions
- stream_start()   -- POST /v2/streams
- stream_stop()    -- POST /v2/streams/{id}/stop

All HTTP calls are intercepted by patching the _raw_* helper methods
directly on the client instance -- no real network traffic.
"""

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: load SDK modules under sthrip_sdk namespace (mirrors other SDK tests)
# ---------------------------------------------------------------------------

_SDK_STHRIP_DIR = Path(__file__).parent.parent / "sdk" / "sthrip"


def _load_sdk_module(alias: str, filename: str):
    """Load an SDK source file under a custom module alias."""
    path = _SDK_STHRIP_DIR / filename
    spec = importlib.util.spec_from_file_location(alias, str(path))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "sthrip_sdk"
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def _bootstrap_sdk():
    """Register a fake sthrip_sdk package so relative imports in client.py work."""
    if "sthrip_sdk" in sys.modules:
        return

    pkg = types.ModuleType("sthrip_sdk")
    pkg.__path__ = [str(_SDK_STHRIP_DIR)]
    pkg.__package__ = "sthrip_sdk"
    sys.modules["sthrip_sdk"] = pkg

    exc_mod = _load_sdk_module("sthrip_sdk.exceptions", "exceptions.py")
    auth_mod = _load_sdk_module("sthrip_sdk.auth", "auth.py")
    client_mod = _load_sdk_module("sthrip_sdk.client", "client.py")

    pkg.exceptions = exc_mod
    pkg.auth = auth_mod
    pkg.client = client_mod


_bootstrap_sdk()

from sthrip_sdk.client import Sthrip  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_client(api_key: str = "test-key-abc", api_url: str = "http://test"):
    """Create a Sthrip client with all network I/O suppressed."""
    with (
        patch("sthrip_sdk.client.load_credentials", return_value=None),
        patch.object(Sthrip, "_auto_register", return_value=api_key),
        patch.dict(os.environ, {"STHRIP_API_KEY": api_key}, clear=False),
    ):
        client = Sthrip(api_key=api_key, api_url=api_url)

    # Replace the underlying session so no real HTTP calls can escape.
    mock_session = MagicMock()
    client._session = mock_session
    return client


# ===========================================================================
# channel_open() tests
# ===========================================================================

class TestChannelOpen:

    def test_calls_post_channels_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={"channel_id": "ch-1"}) as mock:
            client.channel_open("bob-agent", deposit=0.5)
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/channels"

    def test_payload_contains_counterparty_agent_name(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_open("bob-agent", deposit=0.5)
        payload = mock.call_args[1]["json_body"]
        assert payload["counterparty_agent_name"] == "bob-agent"

    def test_deposit_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_open("bob-agent", deposit=1.25)
        payload = mock.call_args[1]["json_body"]
        assert isinstance(payload["deposit"], str)
        assert payload["deposit"] == "1.25"

    def test_default_settlement_period_is_3600(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_open("bob-agent", deposit=0.5)
        payload = mock.call_args[1]["json_body"]
        assert payload["settlement_period"] == 3600

    def test_custom_settlement_period_forwarded(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_open("bob-agent", deposit=0.5, settlement_period=7200)
        payload = mock.call_args[1]["json_body"]
        assert payload["settlement_period"] == 7200

    def test_payload_has_all_three_keys(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_open("carol", deposit=2.0, settlement_period=1800)
        payload = mock.call_args[1]["json_body"]
        assert set(payload.keys()) == {"counterparty_agent_name", "deposit", "settlement_period"}

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"channel_id": "ch-abc", "status": "open"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.channel_open("bob-agent", deposit=0.5)
        assert result == expected

    def test_zero_deposit_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_open("bob-agent", deposit=0)
        payload = mock.call_args[1]["json_body"]
        assert payload["deposit"] == "0"

    def test_large_deposit_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_open("bob-agent", deposit=9999.99)
        payload = mock.call_args[1]["json_body"]
        assert payload["deposit"] == "9999.99"


# ===========================================================================
# channel_settle() tests
# ===========================================================================

class TestChannelSettle:

    def test_calls_post_settle_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={"status": "settled"}) as mock:
            client.channel_settle("ch-1", nonce=5, balance_a=0.3, balance_b=0.7,
                                  signature_a="sigA", signature_b="sigB")
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/channels/ch-1/settle"

    def test_channel_id_interpolated_into_path(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_settle("unique-ch-id", nonce=1, balance_a=0.5, balance_b=0.5,
                                  signature_a="sA", signature_b="sB")
        path_arg = mock.call_args[0][0]
        assert "unique-ch-id" in path_arg

    def test_payload_contains_nonce(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_settle("ch-1", nonce=42, balance_a=0.3, balance_b=0.7,
                                  signature_a="sigA", signature_b="sigB")
        payload = mock.call_args[1]["json_body"]
        assert payload["nonce"] == 42

    def test_balance_a_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_settle("ch-1", nonce=1, balance_a=0.3, balance_b=0.7,
                                  signature_a="sigA", signature_b="sigB")
        payload = mock.call_args[1]["json_body"]
        assert isinstance(payload["balance_a"], str)
        assert payload["balance_a"] == "0.3"

    def test_balance_b_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_settle("ch-1", nonce=1, balance_a=0.3, balance_b=0.7,
                                  signature_a="sigA", signature_b="sigB")
        payload = mock.call_args[1]["json_body"]
        assert isinstance(payload["balance_b"], str)
        assert payload["balance_b"] == "0.7"

    def test_signatures_included_in_payload(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_settle("ch-1", nonce=1, balance_a=0.4, balance_b=0.6,
                                  signature_a="alice-sig", signature_b="bob-sig")
        payload = mock.call_args[1]["json_body"]
        assert payload["signature_a"] == "alice-sig"
        assert payload["signature_b"] == "bob-sig"

    def test_payload_has_all_required_keys(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_settle("ch-1", nonce=3, balance_a=0.5, balance_b=0.5,
                                  signature_a="sA", signature_b="sB")
        payload = mock.call_args[1]["json_body"]
        assert set(payload.keys()) == {"nonce", "balance_a", "balance_b", "signature_a", "signature_b"}

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"channel_id": "ch-1", "status": "settled", "settled_at": "2026-04-01T12:00:00Z"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.channel_settle("ch-1", nonce=1, balance_a=0.5, balance_b=0.5,
                                           signature_a="sA", signature_b="sB")
        assert result == expected


# ===========================================================================
# channel_close() tests
# ===========================================================================

class TestChannelClose:

    def test_calls_post_close_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={"status": "closed"}) as mock:
            client.channel_close("ch-99")
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/channels/ch-99/close"

    def test_channel_id_interpolated_into_path(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_close("channel-xyz-001")
        path_arg = mock.call_args[0][0]
        assert "channel-xyz-001" in path_arg

    def test_no_json_body_sent(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.channel_close("ch-99")
        # _raw_post called with only the path, no json_body kwarg (or None)
        args = mock.call_args[0]
        kwargs = mock.call_args[1]
        assert len(args) == 1  # only path positional arg
        assert kwargs.get("json_body") is None

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"channel_id": "ch-99", "status": "closed"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.channel_close("ch-99")
        assert result == expected


# ===========================================================================
# channels() tests
# ===========================================================================

class TestChannels:

    def test_calls_get_channels_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.channels()
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/channels"

    def test_returns_api_response_list(self):
        client = _build_client()
        expected = [{"channel_id": "ch-1"}, {"channel_id": "ch-2"}]
        with patch.object(client, "_raw_get", return_value=expected):
            result = client.channels()
        assert result == expected

    def test_returns_api_response_dict(self):
        client = _build_client()
        expected = {"channels": [{"channel_id": "ch-1"}], "total": 1}
        with patch.object(client, "_raw_get", return_value=expected):
            result = client.channels()
        assert result == expected

    def test_no_params_sent(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.channels()
        args = mock.call_args[0]
        kwargs = mock.call_args[1]
        # Should only have the path, no extra params
        assert args[0] == "/v2/channels"
        assert kwargs.get("params") is None


# ===========================================================================
# subscribe() tests
# ===========================================================================

class TestSubscribe:

    def test_calls_post_subscriptions_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={"subscription_id": "sub-1"}) as mock:
            client.subscribe("alice", amount=0.01, interval=86400)
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/subscriptions"

    def test_payload_contains_to_agent_name(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.subscribe("alice", amount=0.01, interval=86400)
        payload = mock.call_args[1]["json_body"]
        assert payload["to_agent_name"] == "alice"

    def test_amount_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.subscribe("alice", amount=0.05, interval=86400)
        payload = mock.call_args[1]["json_body"]
        assert isinstance(payload["amount"], str)
        assert payload["amount"] == "0.05"

    def test_interval_included_in_payload(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.subscribe("alice", amount=0.01, interval=3600)
        payload = mock.call_args[1]["json_body"]
        assert payload["interval"] == 3600

    def test_max_payments_excluded_when_none(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.subscribe("alice", amount=0.01, interval=86400)
        payload = mock.call_args[1]["json_body"]
        assert "max_payments" not in payload

    def test_max_payments_included_when_provided(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.subscribe("alice", amount=0.01, interval=86400, max_payments=12)
        payload = mock.call_args[1]["json_body"]
        assert payload["max_payments"] == 12

    def test_base_payload_keys_without_max_payments(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.subscribe("alice", amount=0.01, interval=86400)
        payload = mock.call_args[1]["json_body"]
        assert set(payload.keys()) == {"to_agent_name", "amount", "interval"}

    def test_base_payload_keys_with_max_payments(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.subscribe("alice", amount=0.01, interval=86400, max_payments=6)
        payload = mock.call_args[1]["json_body"]
        assert set(payload.keys()) == {"to_agent_name", "amount", "interval", "max_payments"}

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"subscription_id": "sub-abc", "status": "active"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.subscribe("alice", amount=0.01, interval=86400)
        assert result == expected

    def test_large_amount_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.subscribe("alice", amount=100.0, interval=2592000)
        payload = mock.call_args[1]["json_body"]
        assert payload["amount"] == "100.0"


# ===========================================================================
# unsubscribe() tests
# ===========================================================================

class TestUnsubscribe:

    def test_calls_delete_subscriptions_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_request", return_value={"status": "cancelled"}) as mock:
            client.unsubscribe("sub-42")
        mock.assert_called_once()
        method_arg = mock.call_args[0][0]
        path_arg = mock.call_args[0][1]
        assert method_arg == "DELETE"
        assert path_arg == "/v2/subscriptions/sub-42"

    def test_subscription_id_interpolated_into_path(self):
        client = _build_client()
        with patch.object(client, "_raw_request", return_value={}) as mock:
            client.unsubscribe("unique-sub-999")
        path_arg = mock.call_args[0][1]
        assert "unique-sub-999" in path_arg

    def test_uses_delete_method(self):
        client = _build_client()
        with patch.object(client, "_raw_request", return_value={}) as mock:
            client.unsubscribe("sub-1")
        method_arg = mock.call_args[0][0]
        assert method_arg == "DELETE"

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"subscription_id": "sub-42", "status": "cancelled"}
        with patch.object(client, "_raw_request", return_value=expected):
            result = client.unsubscribe("sub-42")
        assert result == expected


# ===========================================================================
# subscriptions() tests
# ===========================================================================

class TestSubscriptions:

    def test_calls_get_subscriptions_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.subscriptions()
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/subscriptions"

    def test_returns_api_response_list(self):
        client = _build_client()
        expected = [{"subscription_id": "sub-1"}, {"subscription_id": "sub-2"}]
        with patch.object(client, "_raw_get", return_value=expected):
            result = client.subscriptions()
        assert result == expected

    def test_returns_api_response_dict(self):
        client = _build_client()
        expected = {"subscriptions": [{"subscription_id": "sub-1"}], "total": 1}
        with patch.object(client, "_raw_get", return_value=expected):
            result = client.subscriptions()
        assert result == expected

    def test_no_extra_params_sent(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.subscriptions()
        args = mock.call_args[0]
        kwargs = mock.call_args[1]
        assert args[0] == "/v2/subscriptions"
        assert kwargs.get("params") is None


# ===========================================================================
# stream_start() tests
# ===========================================================================

class TestStreamStart:

    def test_calls_post_streams_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={"stream_id": "str-1"}) as mock:
            client.stream_start("ch-abc", rate_per_second=0.001)
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/streams"

    def test_payload_contains_channel_id_as_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.stream_start("ch-uuid-1", rate_per_second=0.001)
        payload = mock.call_args[1]["json_body"]
        assert payload["channel_id"] == "ch-uuid-1"
        assert isinstance(payload["channel_id"], str)

    def test_rate_per_second_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.stream_start("ch-abc", rate_per_second=0.005)
        payload = mock.call_args[1]["json_body"]
        assert isinstance(payload["rate_per_second"], str)
        assert payload["rate_per_second"] == "0.005"

    def test_payload_has_both_keys(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.stream_start("ch-abc", rate_per_second=0.001)
        payload = mock.call_args[1]["json_body"]
        assert set(payload.keys()) == {"channel_id", "rate_per_second"}

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"stream_id": "str-xyz", "status": "streaming"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.stream_start("ch-abc", rate_per_second=0.001)
        assert result == expected

    def test_zero_rate_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.stream_start("ch-abc", rate_per_second=0)
        payload = mock.call_args[1]["json_body"]
        assert payload["rate_per_second"] == "0"


# ===========================================================================
# stream_stop() tests
# ===========================================================================

class TestStreamStop:

    def test_calls_post_stop_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={"status": "stopped"}) as mock:
            client.stream_stop("str-77")
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/streams/str-77/stop"

    def test_stream_id_interpolated_into_path(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.stream_stop("unique-stream-id-999")
        path_arg = mock.call_args[0][0]
        assert "unique-stream-id-999" in path_arg

    def test_no_json_body_sent(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.stream_stop("str-77")
        args = mock.call_args[0]
        kwargs = mock.call_args[1]
        assert len(args) == 1  # only path positional arg
        assert kwargs.get("json_body") is None

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"stream_id": "str-77", "status": "stopped", "final_balance": "0.45"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.stream_stop("str-77")
        assert result == expected
