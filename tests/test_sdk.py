"""Comprehensive unit tests for the Sthrip SDK.

Tests cover:
- auth module (load_credentials, save_credentials)
- exceptions (hierarchy, attributes, defaults)
- client (key resolution, all public methods)
- error mapping (_map_error)
- agent name generation (_generate_agent_name)

No real HTTP calls are made -- requests.Session is mocked throughout.

Import strategy: the SDK lives at sdk/sthrip/ but the repo root also has a
sthrip/ package (the server-side library).  To avoid the namespace clash we
load the SDK modules under the alias ``sthrip_sdk`` using importlib so pytest's
sys.path manipulation cannot interfere.
"""

import importlib.util
import json
import os
import stat
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

# ---------------------------------------------------------------------------
# Bootstrap: load SDK modules under the sthrip_sdk namespace
# ---------------------------------------------------------------------------

_SDK_STHRIP_DIR = Path(__file__).parent.parent / "sdk" / "sthrip"


def _load_sdk_module(alias, filename):
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
        return  # already done

    # Create the package stub
    pkg = types.ModuleType("sthrip_sdk")
    pkg.__path__ = [str(_SDK_STHRIP_DIR)]
    pkg.__package__ = "sthrip_sdk"
    sys.modules["sthrip_sdk"] = pkg

    # Load submodules in dependency order
    exc_mod = _load_sdk_module("sthrip_sdk.exceptions", "exceptions.py")
    auth_mod = _load_sdk_module("sthrip_sdk.auth", "auth.py")
    client_mod = _load_sdk_module("sthrip_sdk.client", "client.py")

    pkg.exceptions = exc_mod
    pkg.auth = auth_mod
    pkg.client = client_mod


_bootstrap_sdk()

# Now import from our aliased namespace
from sthrip_sdk.auth import load_credentials, save_credentials, CREDENTIALS_PATH, _REQUIRED_KEYS  # noqa: E402
from sthrip_sdk.exceptions import (  # noqa: E402
    AgentNotFound,
    AuthError,
    InsufficientBalance,
    NetworkError,
    PaymentError,
    RateLimitError,
    StrhipError,
)
from sthrip_sdk.client import (  # noqa: E402
    Sthrip,
    _generate_agent_name,
    _resolve_api_url,
    _map_error,
    _DEFAULT_API_URL,
    _REQUEST_TIMEOUT,
    _USER_AGENT,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _make_response(status_code, json_data=None, text=""):
    """Build a minimal mock Response object."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no JSON")
    return resp


def _build_client(api_key="test-key-abc", api_url="https://api.example.com"):
    """Create a Sthrip client backed by a mock session, no credential lookup."""
    # Patch the SDK's own load_credentials reference (inside sthrip_sdk.client)
    with patch("sthrip_sdk.client.load_credentials", return_value=None), \
         patch.object(Sthrip, "_auto_register", return_value=api_key), \
         patch.dict(os.environ, {"STHRIP_API_KEY": api_key}, clear=False):
        client = Sthrip(api_key=api_key, api_url=api_url)

    mock_session = MagicMock()
    client._session = mock_session
    return client, mock_session


# ===========================================================================
# 1. Auth module tests
# ===========================================================================

class TestLoadCredentials:

    def test_returns_none_when_file_missing(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        result = load_credentials(path=missing)
        assert result is None

    def test_returns_none_for_malformed_json(self, tmp_path):
        bad_file = tmp_path / "creds.json"
        bad_file.write_text("this is { not valid JSON!!!", encoding="utf-8")
        result = load_credentials(path=bad_file)
        assert result is None

    def test_returns_none_for_json_array_instead_of_object(self, tmp_path):
        arr_file = tmp_path / "creds.json"
        arr_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        result = load_credentials(path=arr_file)
        assert result is None

    def test_returns_none_when_required_key_missing(self, tmp_path):
        # Missing agent_id
        partial = {
            "api_key": "key123",
            "agent_name": "myagent",
            "api_url": "https://example.com",
        }
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps(partial), encoding="utf-8")
        result = load_credentials(path=creds_file)
        assert result is None

    def test_returns_none_when_value_is_null(self, tmp_path):
        data = {
            "api_key": None,
            "agent_id": "agent-001",
            "agent_name": "myagent",
            "api_url": "https://example.com",
        }
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps(data), encoding="utf-8")
        result = load_credentials(path=creds_file)
        assert result is None

    def test_loads_valid_credentials(self, tmp_path):
        data = {
            "api_key": "sk-abc123",
            "agent_id": "agent-001",
            "agent_name": "my-agent",
            "api_url": "https://sthrip.example.com",
            "extra_field": "ignored",
        }
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps(data), encoding="utf-8")
        result = load_credentials(path=creds_file)
        assert result is not None
        assert result["api_key"] == "sk-abc123"
        assert result["agent_id"] == "agent-001"
        assert result["agent_name"] == "my-agent"
        assert result["api_url"] == "https://sthrip.example.com"
        # Unknown fields must not leak through
        assert "extra_field" not in result

    def test_returns_only_required_keys(self, tmp_path):
        data = {k: "value-{}".format(k) for k in _REQUIRED_KEYS}
        data["bonus"] = "should-be-excluded"
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps(data), encoding="utf-8")
        result = load_credentials(path=creds_file)
        assert set(result.keys()) == set(_REQUIRED_KEYS)

    def test_coerces_numeric_values_to_str(self, tmp_path):
        data = {
            "api_key": 99999,
            "agent_id": "agent-001",
            "agent_name": "my-agent",
            "api_url": "https://example.com",
        }
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps(data), encoding="utf-8")
        result = load_credentials(path=creds_file)
        assert result is not None
        assert isinstance(result["api_key"], str)
        assert result["api_key"] == "99999"

    def test_default_path_used_when_no_path_given(self, tmp_path):
        """Verify the function uses CREDENTIALS_PATH when path is omitted."""
        fake_path = tmp_path / ".sthrip" / "credentials.json"
        fake_path.parent.mkdir(parents=True)
        data = {k: "v" for k in _REQUIRED_KEYS}
        fake_path.write_text(json.dumps(data), encoding="utf-8")

        with patch("sthrip_sdk.auth.CREDENTIALS_PATH", fake_path):
            result = load_credentials()
        assert result is not None

    def test_returns_fresh_copy_each_call(self, tmp_path):
        data = {k: "value" for k in _REQUIRED_KEYS}
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps(data), encoding="utf-8")
        result1 = load_credentials(path=creds_file)
        result2 = load_credentials(path=creds_file)
        assert result1 is not result2


class TestSaveCredentials:

    def test_creates_parent_directory(self, tmp_path):
        deep = tmp_path / "a" / "b" / "creds.json"
        save_credentials(
            api_key="k", agent_id="id", agent_name="name",
            api_url="https://x.com", path=deep,
        )
        assert deep.exists()

    def test_written_content_is_valid_json(self, tmp_path):
        creds_file = tmp_path / "creds.json"
        save_credentials(
            api_key="mykey", agent_id="ag-1", agent_name="alice",
            api_url="https://example.com", path=creds_file,
        )
        data = json.loads(creds_file.read_text(encoding="utf-8"))
        assert data["api_key"] == "mykey"
        assert data["agent_id"] == "ag-1"
        assert data["agent_name"] == "alice"
        assert data["api_url"] == "https://example.com"

    def test_sets_0600_permissions(self, tmp_path):
        creds_file = tmp_path / "creds.json"
        save_credentials(
            api_key="k", agent_id="i", agent_name="n",
            api_url="u", path=creds_file,
        )
        mode = stat.S_IMODE(os.stat(str(creds_file)).st_mode)
        assert mode == 0o600

    def test_overwrites_existing_file(self, tmp_path):
        creds_file = tmp_path / "creds.json"
        save_credentials(api_key="old", agent_id="i", agent_name="n", api_url="u", path=creds_file)
        save_credentials(api_key="new", agent_id="i", agent_name="n", api_url="u", path=creds_file)
        data = json.loads(creds_file.read_text(encoding="utf-8"))
        assert data["api_key"] == "new"

    def test_content_roundtrips_through_load(self, tmp_path):
        creds_file = tmp_path / "creds.json"
        save_credentials(
            api_key="roundtrip-key", agent_id="ag-99",
            agent_name="roundtrip-agent", api_url="https://rt.example.com",
            path=creds_file,
        )
        loaded = load_credentials(path=creds_file)
        assert loaded == {
            "api_key": "roundtrip-key",
            "agent_id": "ag-99",
            "agent_name": "roundtrip-agent",
            "api_url": "https://rt.example.com",
        }

    def test_chmod_oserror_is_silently_ignored(self, tmp_path):
        """save_credentials must not raise when chmod fails (e.g. Windows)."""
        creds_file = tmp_path / "creds.json"
        with patch("os.chmod", side_effect=OSError("chmod not supported")):
            save_credentials(
                api_key="k", agent_id="i", agent_name="n",
                api_url="u", path=creds_file,
            )
        assert creds_file.exists()


# ===========================================================================
# 2. Exception hierarchy tests
# ===========================================================================

class TestExceptionHierarchy:

    def test_sthrip_error_is_exception(self):
        assert issubclass(StrhipError, Exception)

    def test_auth_error_inherits_sthrip_error(self):
        assert issubclass(AuthError, StrhipError)

    def test_payment_error_inherits_sthrip_error(self):
        assert issubclass(PaymentError, StrhipError)

    def test_insufficient_balance_inherits_payment_error(self):
        assert issubclass(InsufficientBalance, PaymentError)

    def test_agent_not_found_inherits_payment_error(self):
        assert issubclass(AgentNotFound, PaymentError)

    def test_rate_limit_error_inherits_sthrip_error(self):
        assert issubclass(RateLimitError, StrhipError)

    def test_network_error_inherits_sthrip_error(self):
        assert issubclass(NetworkError, StrhipError)

    def test_insufficient_balance_catchable_as_payment_error(self):
        with pytest.raises(PaymentError):
            raise InsufficientBalance("low funds")

    def test_agent_not_found_catchable_as_sthrip_error(self):
        with pytest.raises(StrhipError):
            raise AgentNotFound("no such agent")


class TestExceptionAttributes:

    def test_sthrip_error_stores_detail_and_status_code(self):
        exc = StrhipError("something went wrong", 500)
        assert exc.detail == "something went wrong"
        assert exc.status_code == 500

    def test_sthrip_error_status_code_defaults_to_none(self):
        exc = StrhipError("oops")
        assert exc.status_code is None

    def test_auth_error_defaults(self):
        exc = AuthError()
        assert exc.detail == "Authentication failed"
        assert exc.status_code == 401

    def test_auth_error_custom_values(self):
        exc = AuthError("forbidden", 403)
        assert exc.detail == "forbidden"
        assert exc.status_code == 403

    def test_payment_error_defaults(self):
        exc = PaymentError()
        assert exc.detail == "Payment failed"
        assert exc.status_code is None

    def test_insufficient_balance_defaults(self):
        exc = InsufficientBalance()
        assert exc.detail == "Insufficient balance"
        assert exc.status_code is None

    def test_insufficient_balance_custom_values(self):
        exc = InsufficientBalance("Not enough XMR", 402)
        assert exc.detail == "Not enough XMR"
        assert exc.status_code == 402

    def test_agent_not_found_defaults(self):
        exc = AgentNotFound()
        assert exc.detail == "Agent not found"
        assert exc.status_code == 404

    def test_rate_limit_error_defaults(self):
        exc = RateLimitError()
        assert exc.detail == "Rate limit exceeded"
        assert exc.status_code == 429

    def test_network_error_defaults(self):
        exc = NetworkError()
        assert exc.detail == "Network error"
        assert exc.status_code is None

    def test_exception_message_equals_detail(self):
        exc = StrhipError("test detail", 200)
        assert str(exc) == "test detail"


# ===========================================================================
# 3. _map_error tests
# ===========================================================================

class TestMapError:

    def test_429_maps_to_rate_limit_error(self):
        exc = _map_error(429, "Too Many Requests")
        assert isinstance(exc, RateLimitError)
        assert exc.status_code == 429

    def test_401_maps_to_auth_error(self):
        exc = _map_error(401, "Unauthorized")
        assert isinstance(exc, AuthError)
        assert exc.status_code == 401

    def test_403_maps_to_auth_error(self):
        exc = _map_error(403, "Forbidden")
        assert isinstance(exc, AuthError)
        assert exc.status_code == 403

    def test_404_with_agent_in_detail_maps_to_agent_not_found(self):
        exc = _map_error(404, "agent alice does not exist")
        assert isinstance(exc, AgentNotFound)
        assert exc.status_code == 404

    def test_404_without_agent_maps_to_payment_error(self):
        exc = _map_error(404, "resource not found")
        assert isinstance(exc, PaymentError)
        assert exc.status_code == 404

    def test_insufficient_in_detail_maps_to_insufficient_balance(self):
        exc = _map_error(400, "Insufficient balance for transfer")
        assert isinstance(exc, InsufficientBalance)

    def test_not_enough_in_detail_maps_to_insufficient_balance(self):
        exc = _map_error(400, "not enough XMR in wallet")
        assert isinstance(exc, InsufficientBalance)

    def test_generic_4xx_maps_to_payment_error(self):
        exc = _map_error(422, "Validation error")
        assert isinstance(exc, PaymentError)
        assert exc.status_code == 422

    def test_5xx_maps_to_sthrip_error(self):
        exc = _map_error(500, "Internal Server Error")
        assert isinstance(exc, StrhipError)
        assert exc.status_code == 500

    def test_detail_is_preserved_on_mapped_error(self):
        detail = "Custom error detail text"
        exc = _map_error(401, detail)
        assert exc.detail == detail

    def test_case_insensitive_agent_matching(self):
        exc = _map_error(404, "Agent not found")
        assert isinstance(exc, AgentNotFound)

    def test_case_insensitive_insufficient_matching(self):
        exc = _map_error(400, "INSUFFICIENT funds available")
        assert isinstance(exc, InsufficientBalance)

    def test_sub_400_non_error_status_falls_back_to_sthrip_error(self):
        # status_code < 400 and no special keyword -- hits the final fallthrough
        exc = _map_error(301, "Moved Permanently")
        assert type(exc) is StrhipError
        assert exc.status_code == 301


# ===========================================================================
# 4. _generate_agent_name tests
# ===========================================================================

class TestGenerateAgentName:

    def test_returns_string(self):
        name = _generate_agent_name()
        assert isinstance(name, str)

    def test_contains_hyphen_separator(self):
        name = _generate_agent_name()
        assert "-" in name

    def test_suffix_is_8_hex_chars(self):
        name = _generate_agent_name()
        suffix = name.rsplit("-", 1)[-1]
        assert len(suffix) == 8
        int(suffix, 16)  # must be valid hex

    def test_uses_hostname_prefix(self):
        with patch("sthrip_sdk.client.socket.gethostname", return_value="myserver.internal"):
            name = _generate_agent_name()
        assert name.startswith("myserver-")

    def test_strips_domain_from_hostname(self):
        with patch("sthrip_sdk.client.socket.gethostname", return_value="box01.corp.example.com"):
            name = _generate_agent_name()
        assert name.startswith("box01-")

    def test_falls_back_to_agent_when_hostname_raises(self):
        with patch("sthrip_sdk.client.socket.gethostname", side_effect=OSError("no hostname")):
            name = _generate_agent_name()
        assert name.startswith("agent-")

    def test_falls_back_to_agent_when_hostname_all_special_chars(self):
        with patch("sthrip_sdk.client.socket.gethostname", return_value="!!!###$$$"):
            name = _generate_agent_name()
        assert name.startswith("agent-")

    def test_removes_non_alphanumeric_non_hyphen_non_underscore(self):
        with patch("sthrip_sdk.client.socket.gethostname", return_value="my host!@#"):
            name = _generate_agent_name()
        prefix = name.rsplit("-", 1)[0]
        for ch in prefix:
            assert ch.isalnum() or ch in ("_", "-")

    def test_each_call_generates_unique_name(self):
        names = {_generate_agent_name() for _ in range(20)}
        assert len(names) == 20


# ===========================================================================
# 5. _resolve_api_url tests
# ===========================================================================

class TestResolveApiUrl:

    def test_returns_explicit_url_when_provided(self):
        assert _resolve_api_url("https://custom.api.com") == "https://custom.api.com"

    def test_strips_trailing_slash(self):
        assert _resolve_api_url("https://api.example.com/") == "https://api.example.com"

    def test_uses_env_var_when_no_explicit(self):
        with patch.dict(os.environ, {"STHRIP_API_URL": "https://env.api.com"}):
            result = _resolve_api_url("")
        assert result == "https://env.api.com"

    def test_falls_back_to_default_url(self):
        env_without = {k: v for k, v in os.environ.items() if k != "STHRIP_API_URL"}
        with patch.dict(os.environ, env_without, clear=True):
            result = _resolve_api_url("")
        assert result == _DEFAULT_API_URL

    def test_explicit_overrides_env(self):
        with patch.dict(os.environ, {"STHRIP_API_URL": "https://env.api.com"}):
            result = _resolve_api_url("https://explicit.api.com")
        assert result == "https://explicit.api.com"


# ===========================================================================
# 6. Client constructor / key resolution tests
# ===========================================================================

class TestClientKeyResolution:

    def test_explicit_api_key_used_directly(self):
        with patch("sthrip_sdk.client.load_credentials", return_value=None):
            client = Sthrip(api_key="explicit-key-123", api_url="https://api.test")
        assert client._api_key == "explicit-key-123"

    def test_env_var_used_when_no_explicit_key(self):
        env = {k: v for k, v in os.environ.items() if k != "STHRIP_API_KEY"}
        env["STHRIP_API_KEY"] = "env-key-456"
        with patch.dict(os.environ, env, clear=True), \
             patch("sthrip_sdk.client.load_credentials", return_value=None):
            client = Sthrip(api_url="https://api.test")
        assert client._api_key == "env-key-456"

    def test_credentials_file_used_when_no_env_var(self):
        creds = {
            "api_key": "file-key-789",
            "agent_id": "ag-1",
            "agent_name": "myagent",
            "api_url": "https://api.test",
        }
        env_without_key = {k: v for k, v in os.environ.items() if k != "STHRIP_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True), \
             patch("sthrip_sdk.client.load_credentials", return_value=creds):
            client = Sthrip(api_url="https://api.test")
        assert client._api_key == "file-key-789"

    def test_auto_register_called_when_no_key_found(self):
        env_without_key = {k: v for k, v in os.environ.items() if k != "STHRIP_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True), \
             patch("sthrip_sdk.client.load_credentials", return_value=None), \
             patch.object(Sthrip, "_auto_register", return_value="auto-key") as mock_reg:
            client = Sthrip(api_url="https://api.test")
        mock_reg.assert_called_once()
        assert client._api_key == "auto-key"

    def test_explicit_key_takes_priority_over_env_var(self):
        with patch.dict(os.environ, {"STHRIP_API_KEY": "env-key"}, clear=False), \
             patch("sthrip_sdk.client.load_credentials", return_value=None):
            client = Sthrip(api_key="explicit-key", api_url="https://api.test")
        assert client._api_key == "explicit-key"

    def test_session_has_correct_user_agent(self):
        with patch("sthrip_sdk.client.load_credentials", return_value=None), \
             patch.object(Sthrip, "_auto_register", return_value="k"):
            client = Sthrip(api_url="https://api.test")
        assert client._session.headers["User-Agent"] == _USER_AGENT

    def test_session_has_accept_json_header(self):
        with patch("sthrip_sdk.client.load_credentials", return_value=None), \
             patch.object(Sthrip, "_auto_register", return_value="k"):
            client = Sthrip(api_url="https://api.test")
        assert client._session.headers["Accept"] == "application/json"

    def test_api_url_trailing_slash_removed(self):
        with patch("sthrip_sdk.client.load_credentials", return_value=None):
            client = Sthrip(api_key="k", api_url="https://api.test/")
        assert client._api_url == "https://api.test"


# ===========================================================================
# 7. Auto-registration tests
# ===========================================================================

class TestAutoRegister:

    def test_posts_to_register_endpoint(self):
        client, mock_session = _build_client()
        reg_response = _make_response(200, {
            "api_key": "new-key", "agent_id": "ag-new", "agent_name": "newagent",
        })
        mock_session.request.return_value = reg_response

        with patch("sthrip_sdk.client.save_credentials"), \
             patch("sthrip_sdk.client._generate_agent_name", return_value="newagent"):
            result = client._auto_register()

        call_kwargs = mock_session.request.call_args
        assert call_kwargs[0][1].endswith("/v2/agents/register")
        assert result == "new-key"

    def test_saves_credentials_after_registration(self):
        client, mock_session = _build_client()
        reg_response = _make_response(200, {
            "api_key": "saved-key", "agent_id": "ag-saved", "agent_name": "savedagent",
        })
        mock_session.request.return_value = reg_response

        with patch("sthrip_sdk.client.save_credentials") as mock_save, \
             patch("sthrip_sdk.client._generate_agent_name", return_value="savedagent"):
            client._auto_register()

        mock_save.assert_called_once_with(
            api_key="saved-key",
            agent_id="ag-saved",
            agent_name="savedagent",
            api_url=client._api_url,
        )

    def test_sends_medium_privacy_level(self):
        client, mock_session = _build_client()
        reg_response = _make_response(200, {
            "api_key": "k", "agent_id": "i", "agent_name": "n",
        })
        mock_session.request.return_value = reg_response

        with patch("sthrip_sdk.client.save_credentials"), \
             patch("sthrip_sdk.client._generate_agent_name", return_value="n"):
            client._auto_register()

        sent_body = mock_session.request.call_args[1]["json"]
        assert sent_body["privacy_level"] == "medium"

    def test_registration_is_unauthenticated(self):
        client, mock_session = _build_client()
        reg_response = _make_response(200, {
            "api_key": "k", "agent_id": "i", "agent_name": "n",
        })
        mock_session.request.return_value = reg_response

        with patch("sthrip_sdk.client.save_credentials"), \
             patch("sthrip_sdk.client._generate_agent_name", return_value="n"):
            client._auto_register()

        sent_headers = mock_session.request.call_args[1]["headers"]
        assert "Authorization" not in sent_headers


# ===========================================================================
# 8. deposit_address() tests
# ===========================================================================

class TestDepositAddress:

    def test_returns_address_string(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(
            200, {"deposit_address": "44AFFq5kSiGBo...XMR"}
        )
        result = client.deposit_address()
        assert result == "44AFFq5kSiGBo...XMR"

    def test_posts_to_correct_endpoint(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(
            200, {"deposit_address": "addr"}
        )
        client.deposit_address()
        args = mock_session.request.call_args
        assert args[0][0] == "POST"
        assert args[0][1].endswith("/v2/balance/deposit")

    def test_sends_empty_json_body(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(
            200, {"deposit_address": "addr"}
        )
        client.deposit_address()
        assert mock_session.request.call_args[1]["json"] == {}

    def test_raises_auth_error_on_401(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(401, {"detail": "Unauthorized"})
        with pytest.raises(AuthError):
            client.deposit_address()

    def test_raises_rate_limit_on_429(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(429, {"detail": "Too many"})
        with pytest.raises(RateLimitError):
            client.deposit_address()


# ===========================================================================
# 9. pay() tests
# ===========================================================================

class TestPay:

    def _receipt(self):
        return {
            "payment_id": "pay-001",
            "to_agent": "alice",
            "amount": "0.05",
            "status": "completed",
        }

    def test_returns_receipt_dict(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        result = client.pay("alice", 0.05)
        assert result["payment_id"] == "pay-001"

    def test_sends_to_agent_name(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.pay("alice", 0.05)
        body = mock_session.request.call_args[1]["json"]
        assert body["to_agent_name"] == "alice"

    def test_amount_converted_to_string(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.pay("alice", 0.05)
        body = mock_session.request.call_args[1]["json"]
        assert body["amount"] == "0.05"

    def test_default_urgency_is_normal(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.pay("alice", 0.05)
        body = mock_session.request.call_args[1]["json"]
        assert body["urgency"] == "normal"

    def test_memo_included_when_provided(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.pay("alice", 0.05, memo="thanks for the work")
        body = mock_session.request.call_args[1]["json"]
        assert body["memo"] == "thanks for the work"

    def test_memo_absent_when_not_provided(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.pay("alice", 0.05)
        body = mock_session.request.call_args[1]["json"]
        assert "memo" not in body

    def test_posts_to_hub_routing_endpoint(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.pay("alice", 0.1)
        args = mock_session.request.call_args
        assert args[0][0] == "POST"
        assert args[0][1].endswith("/v2/payments/hub-routing")

    def test_raises_insufficient_balance(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(
            400, {"detail": "Insufficient balance for transfer"}
        )
        with pytest.raises(InsufficientBalance):
            client.pay("alice", 999.0)

    def test_raises_agent_not_found(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(
            404, {"detail": "agent 'nobody' not found"}
        )
        with pytest.raises(AgentNotFound):
            client.pay("nobody", 0.01)

    def test_sends_auth_header(self):
        client, mock_session = _build_client(api_key="pay-token")
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.pay("alice", 0.05)
        headers = mock_session.request.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer pay-token"


# ===========================================================================
# 10. balance() tests
# ===========================================================================

class TestBalance:

    def test_returns_balance_dict(self):
        client, mock_session = _build_client()
        balance_data = {
            "available": "1.5",
            "pending": "0.0",
            "total_deposited": "2.0",
            "total_withdrawn": "0.5",
        }
        mock_session.request.return_value = _make_response(200, balance_data)
        result = client.balance()
        assert result == balance_data

    def test_gets_correct_endpoint(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, {"available": "0"})
        client.balance()
        args = mock_session.request.call_args
        assert args[0][0] == "GET"
        assert args[0][1].endswith("/v2/balance")

    def test_sends_auth_header(self):
        client, mock_session = _build_client(api_key="my-test-key")
        mock_session.request.return_value = _make_response(200, {"available": "0"})
        client.balance()
        headers = mock_session.request.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer my-test-key"


# ===========================================================================
# 11. find_agents() tests
# ===========================================================================

class TestFindAgents:

    def test_returns_list_when_api_returns_list(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, [{"name": "alice"}])
        result = client.find_agents()
        assert result == [{"name": "alice"}]

    def test_extracts_agents_key_from_envelope(self):
        client, mock_session = _build_client()
        envelope = {"agents": [{"name": "bob"}], "total": 1}
        mock_session.request.return_value = _make_response(200, envelope)
        result = client.find_agents()
        assert result == [{"name": "bob"}]

    def test_returns_raw_data_when_neither_list_nor_envelope(self):
        client, mock_session = _build_client()
        raw = {"unexpected": "format"}
        mock_session.request.return_value = _make_response(200, raw)
        result = client.find_agents()
        assert result == raw

    def test_passes_kwargs_as_query_params(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, [])
        client.find_agents(limit=10, min_trust_score=0.8, tier="premium")
        params = mock_session.request.call_args[1]["params"]
        assert params["limit"] == 10
        assert params["min_trust_score"] == 0.8
        assert params["tier"] == "premium"

    def test_none_kwargs_excluded_from_params(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, [])
        client.find_agents(limit=None, tier=None)
        params = mock_session.request.call_args[1]["params"]
        assert "limit" not in params
        assert "tier" not in params

    def test_unauthenticated_request(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, [])
        client.find_agents()
        headers = mock_session.request.call_args[1]["headers"]
        assert "Authorization" not in headers

    def test_gets_agents_endpoint(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, [])
        client.find_agents()
        args = mock_session.request.call_args
        assert args[0][0] == "GET"
        assert args[0][1].endswith("/v2/agents")


# ===========================================================================
# 12. me() tests
# ===========================================================================

class TestMe:

    def test_returns_profile_dict(self):
        client, mock_session = _build_client()
        profile = {"agent_id": "ag-1", "agent_name": "alice", "trust_score": 0.9}
        mock_session.request.return_value = _make_response(200, profile)
        result = client.me()
        assert result == profile

    def test_gets_correct_endpoint(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, {"agent_id": "x"})
        client.me()
        args = mock_session.request.call_args
        assert args[0][0] == "GET"
        assert args[0][1].endswith("/v2/me")

    def test_sends_auth_header(self):
        client, mock_session = _build_client(api_key="bearer-token")
        mock_session.request.return_value = _make_response(200, {"agent_id": "x"})
        client.me()
        headers = mock_session.request.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer bearer-token"


# ===========================================================================
# 13. withdraw() tests
# ===========================================================================

class TestWithdraw:

    def _receipt(self):
        return {"tx_hash": "abcdef01", "amount": "0.5", "fee": "0.001"}

    def test_returns_receipt(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        result = client.withdraw(0.5, "4Abc...MoneroAddress")
        assert result["tx_hash"] == "abcdef01"

    def test_sends_amount_as_string(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.withdraw(1.25, "4Abc...MoneroAddress")
        body = mock_session.request.call_args[1]["json"]
        assert body["amount"] == "1.25"

    def test_sends_address(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.withdraw(0.5, "4Abc...MoneroAddress")
        body = mock_session.request.call_args[1]["json"]
        assert body["address"] == "4Abc...MoneroAddress"

    def test_posts_to_withdraw_endpoint(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.withdraw(0.5, "addr")
        args = mock_session.request.call_args
        assert args[0][0] == "POST"
        assert args[0][1].endswith("/v2/balance/withdraw")

    def test_raises_insufficient_balance_on_low_funds(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(
            400, {"detail": "insufficient balance for withdrawal"}
        )
        with pytest.raises(InsufficientBalance):
            client.withdraw(100.0, "addr")


# ===========================================================================
# 14. payment_history() tests
# ===========================================================================

class TestPaymentHistory:

    def test_returns_list_when_api_returns_list(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, [{"id": "p1"}])
        result = client.payment_history()
        assert result == [{"id": "p1"}]

    def test_extracts_payments_key_from_envelope(self):
        client, mock_session = _build_client()
        envelope = {"payments": [{"id": "p2"}], "total": 1}
        mock_session.request.return_value = _make_response(200, envelope)
        result = client.payment_history()
        assert result == [{"id": "p2"}]

    def test_returns_raw_data_when_unknown_shape(self):
        client, mock_session = _build_client()
        raw = {"unknown": "shape"}
        mock_session.request.return_value = _make_response(200, raw)
        result = client.payment_history()
        assert result == raw

    def test_default_limit_is_50(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, [])
        client.payment_history()
        params = mock_session.request.call_args[1]["params"]
        assert params["limit"] == 50

    def test_direction_filter_included_when_given(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, [])
        client.payment_history(direction="in")
        params = mock_session.request.call_args[1]["params"]
        assert params["direction"] == "in"

    def test_direction_absent_when_none(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, [])
        client.payment_history(direction=None)
        params = mock_session.request.call_args[1]["params"]
        assert "direction" not in params

    def test_custom_limit_forwarded(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, [])
        client.payment_history(limit=10)
        params = mock_session.request.call_args[1]["params"]
        assert params["limit"] == 10

    def test_out_direction_filter(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, [])
        client.payment_history(direction="out")
        params = mock_session.request.call_args[1]["params"]
        assert params["direction"] == "out"

    def test_gets_history_endpoint(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, [])
        client.payment_history()
        args = mock_session.request.call_args
        assert args[0][0] == "GET"
        assert args[0][1].endswith("/v2/payments/history")


# ===========================================================================
# 15. Network error mapping tests
# ===========================================================================

class TestNetworkErrors:

    def test_connection_error_raises_network_error(self):
        client, mock_session = _build_client()
        mock_session.request.side_effect = requests.ConnectionError("refused")
        with pytest.raises(NetworkError) as exc_info:
            client.balance()
        assert "Connection failed" in exc_info.value.detail

    def test_timeout_raises_network_error(self):
        client, mock_session = _build_client()
        mock_session.request.side_effect = requests.Timeout("timed out")
        with pytest.raises(NetworkError) as exc_info:
            client.balance()
        assert "timed out" in exc_info.value.detail.lower()

    def test_generic_request_exception_raises_network_error(self):
        client, mock_session = _build_client()
        mock_session.request.side_effect = requests.RequestException("generic failure")
        with pytest.raises(NetworkError) as exc_info:
            client.balance()
        assert "Request error" in exc_info.value.detail

    def test_network_error_has_no_status_code(self):
        client, mock_session = _build_client()
        mock_session.request.side_effect = requests.ConnectionError("down")
        with pytest.raises(NetworkError) as exc_info:
            client.balance()
        assert exc_info.value.status_code is None


# ===========================================================================
# 16. _handle_response edge cases
# ===========================================================================

class TestHandleResponse:

    def test_non_json_error_body_uses_response_text(self):
        client, mock_session = _build_client()
        resp = _make_response(500, json_data=None, text="Internal Server Error")
        mock_session.request.return_value = resp
        with pytest.raises(StrhipError) as exc_info:
            client.balance()
        assert "Internal Server Error" in exc_info.value.detail

    def test_json_error_body_extracts_detail_field(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(
            422, {"detail": "field required"}
        )
        with pytest.raises(PaymentError) as exc_info:
            client.balance()
        assert exc_info.value.detail == "field required"

    def test_successful_response_returns_json(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, {"key": "value"})
        result = client._raw_get("/v2/test")
        assert result == {"key": "value"}

    def test_request_uses_correct_timeout(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, {})
        client.balance()
        call_kwargs = mock_session.request.call_args[1]
        assert call_kwargs["timeout"] == _REQUEST_TIMEOUT

    def test_authenticated_request_adds_bearer_token(self):
        client, mock_session = _build_client(api_key="secret-token")
        mock_session.request.return_value = _make_response(200, {})
        client._raw_get("/v2/test", authenticated=True)
        headers = mock_session.request.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer secret-token"

    def test_unauthenticated_request_omits_auth_header(self):
        client, mock_session = _build_client(api_key="secret-token")
        mock_session.request.return_value = _make_response(200, [])
        client._raw_get("/v2/agents", authenticated=False)
        headers = mock_session.request.call_args[1]["headers"]
        assert "Authorization" not in headers

    def test_headers_returns_new_dict_each_call(self):
        """_headers must return a fresh dict every time (immutability)."""
        client, _ = _build_client()
        h1 = client._headers(True)
        h2 = client._headers(True)
        assert h1 is not h2

    def test_rate_limit_error_on_429(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(
            429, {"detail": "Too Many Requests"}
        )
        with pytest.raises(RateLimitError):
            client.me()

    def test_url_is_constructed_correctly(self):
        client, mock_session = _build_client(api_url="https://custom.api.test")
        mock_session.request.return_value = _make_response(200, {"available": "0"})
        client.balance()
        called_url = mock_session.request.call_args[0][1]
        assert called_url == "https://custom.api.test/v2/balance"


# ===========================================================================
# 17. Session ID tests
# ===========================================================================

class TestSessionId:

    def test_session_id_generated(self):
        """Every client instance gets a unique session UUID."""
        client, _ = _build_client()
        assert client._session_id is not None
        # Validate it is a valid UUID (will raise ValueError if not)
        import uuid as _uuid
        _uuid.UUID(client._session_id)

    def test_session_id_unique_per_instance(self):
        c1, _ = _build_client()
        c2, _ = _build_client()
        assert c1._session_id != c2._session_id

    def test_session_header_sent_on_requests(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, {"available": "0"})
        client.balance()
        headers = mock_session.request.call_args[1]["headers"]
        assert "X-Sthrip-Session" in headers
        assert headers["X-Sthrip-Session"] == client._session_id

    def test_session_spent_starts_at_zero(self):
        from decimal import Decimal
        client, _ = _build_client()
        assert client._session_spent == Decimal("0")


# ===========================================================================
# 18. would_exceed() tests
# ===========================================================================

class TestWouldExceed:

    def test_would_exceed_max_per_tx_returns_true(self):
        """Returns True when amount exceeds max_per_tx."""
        client, _ = _build_client()
        from decimal import Decimal
        client._max_per_tx = Decimal("1.0")
        assert client.would_exceed(1.5) is True

    def test_would_exceed_max_per_tx_returns_false_at_limit(self):
        client, _ = _build_client()
        from decimal import Decimal
        client._max_per_tx = Decimal("1.0")
        assert client.would_exceed(1.0) is False

    def test_would_exceed_max_per_tx_returns_false_below_limit(self):
        client, _ = _build_client()
        from decimal import Decimal
        client._max_per_tx = Decimal("1.0")
        assert client.would_exceed(0.5) is False

    def test_would_exceed_session_returns_true(self):
        """Returns True when session total would be exceeded."""
        client, _ = _build_client()
        from decimal import Decimal
        client._max_per_session = Decimal("5.0")
        client._session_spent = Decimal("4.0")
        assert client.would_exceed(1.5) is True

    def test_would_exceed_session_returns_false_at_limit(self):
        client, _ = _build_client()
        from decimal import Decimal
        client._max_per_session = Decimal("5.0")
        client._session_spent = Decimal("4.0")
        assert client.would_exceed(1.0) is False

    def test_would_exceed_session_returns_false_below_limit(self):
        client, _ = _build_client()
        from decimal import Decimal
        client._max_per_session = Decimal("5.0")
        client._session_spent = Decimal("2.0")
        assert client.would_exceed(1.0) is False

    def test_would_exceed_no_limits_returns_false(self):
        """Returns False when no limits are configured."""
        client, _ = _build_client()
        client._max_per_tx = None
        client._max_per_session = None
        assert client.would_exceed(1000.0) is False

    def test_would_exceed_both_tx_and_session(self):
        """Returns True if either limit is exceeded (tx limit first)."""
        client, _ = _build_client()
        from decimal import Decimal
        client._max_per_tx = Decimal("0.5")
        client._max_per_session = Decimal("100.0")
        assert client.would_exceed(0.6) is True


# ===========================================================================
# 19. Spending policy sync tests
# ===========================================================================

class TestSpendingPolicySync:

    def test_spending_policy_synced_on_init(self):
        """When policy params are set, PUT /v2/me/spending-policy is called."""
        with patch("sthrip_sdk.client.load_credentials", return_value=None), \
             patch.object(Sthrip, "_auto_register", return_value="k"), \
             patch.dict(os.environ, {"STHRIP_API_KEY": "k"}, clear=False):
            client = Sthrip(
                api_key="k",
                api_url="https://api.test",
                max_per_tx=1.0,
                daily_limit=10.0,
            )

        mock_session = MagicMock()
        # The sync already ran during __init__, so we test it directly
        client._session = mock_session
        mock_session.request.return_value = _make_response(200, {"status": "ok"})
        client._sync_spending_policy()

        call_args = mock_session.request.call_args
        assert call_args[0][0] == "PUT"
        assert call_args[0][1].endswith("/v2/me/spending-policy")
        body = call_args[1]["json"]
        assert body["max_per_tx"] == "1.0"
        assert body["daily_limit"] == "10.0"

    def test_spending_policy_not_synced_without_params(self):
        """No HTTP call when no policy params are set."""
        client, mock_session = _build_client()
        client._max_per_tx = None
        client._max_per_session = None
        client._daily_limit = None
        client._allowed_agents = None
        client._require_escrow_above = None
        client._sync_spending_policy()
        mock_session.request.assert_not_called()

    def test_spending_policy_sync_includes_allowed_agents(self):
        client, mock_session = _build_client()
        client._allowed_agents = ["agent-*", "trusted-bot"]
        mock_session.request.return_value = _make_response(200, {"status": "ok"})
        client._sync_spending_policy()
        body = mock_session.request.call_args[1]["json"]
        assert body["allowed_agents"] == ["agent-*", "trusted-bot"]

    def test_spending_policy_sync_graceful_on_error(self):
        """Sync should not raise even if the server returns an error."""
        client, mock_session = _build_client()
        from decimal import Decimal
        client._max_per_tx = Decimal("1.0")
        mock_session.request.return_value = _make_response(
            500, {"detail": "Internal Server Error"}
        )
        # Should not raise
        client._sync_spending_policy()

    def test_set_spending_policy_method(self):
        """set_spending_policy calls PUT and updates local state."""
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, {
            "max_per_tx": "2.0", "is_active": True,
        })
        result = client.set_spending_policy(max_per_tx=2.0)
        call_args = mock_session.request.call_args
        assert call_args[0][0] == "PUT"
        assert call_args[0][1].endswith("/v2/me/spending-policy")
        from decimal import Decimal
        assert client._max_per_tx == Decimal("2.0")

    def test_get_spending_policy_method(self):
        client, mock_session = _build_client()
        policy = {"max_per_tx": "1.0", "daily_limit": "10.0", "is_active": True}
        mock_session.request.return_value = _make_response(200, policy)
        result = client.get_spending_policy()
        assert result == policy
        call_args = mock_session.request.call_args
        assert call_args[0][0] == "GET"
        assert call_args[0][1].endswith("/v2/me/spending-policy")


# ===========================================================================
# 20. Pay with session tracking tests
# ===========================================================================

class TestPayWithSessionTracking:

    def _receipt(self):
        return {
            "payment_id": "pay-001",
            "to_agent": "alice",
            "amount": "0.05",
            "status": "completed",
        }

    def test_pay_increments_session_spent(self):
        from decimal import Decimal
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.pay("alice", 0.05)
        assert client._session_spent == Decimal("0.05")

    def test_pay_increments_session_spent_cumulatively(self):
        from decimal import Decimal
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.pay("alice", 0.05)
        client.pay("alice", 0.10)
        assert client._session_spent == Decimal("0.15")

    def test_pay_blocked_by_would_exceed(self):
        from decimal import Decimal
        client, mock_session = _build_client()
        client._max_per_tx = Decimal("0.01")
        with pytest.raises(PaymentError, match="spending policy"):
            client.pay("alice", 0.05)
        # Session spent should NOT increment on blocked payment
        assert client._session_spent == Decimal("0")

    def test_pay_blocked_by_session_limit(self):
        from decimal import Decimal
        client, mock_session = _build_client()
        client._max_per_session = Decimal("0.10")
        mock_session.request.return_value = _make_response(200, self._receipt())
        client.pay("alice", 0.05)
        client.pay("alice", 0.04)
        # Next one would bring total to 0.10 which equals limit, but 0.05 would exceed
        with pytest.raises(PaymentError, match="spending policy"):
            client.pay("alice", 0.05)
        assert client._session_spent == Decimal("0.09")


# ===========================================================================
# 21. Encryption key registration tests
# ===========================================================================

class TestRegisterEncryptionKey:

    def test_register_encryption_key_calls_put(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, {
            "status": "ok", "public_key": "base64key==",
        })
        result = client.register_encryption_key("base64key==")
        call_args = mock_session.request.call_args
        assert call_args[0][0] == "PUT"
        assert call_args[0][1].endswith("/v2/me/encryption-key")
        body = call_args[1]["json"]
        assert body["public_key"] == "base64key=="
        assert result["status"] == "ok"

    def test_register_encryption_key_sends_auth(self):
        client, mock_session = _build_client(api_key="my-key")
        mock_session.request.return_value = _make_response(200, {"status": "ok"})
        client.register_encryption_key("pk==")
        headers = mock_session.request.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer my-key"


# ===========================================================================
# 22. Get agent public key tests
# ===========================================================================

class TestGetAgentPublicKey:

    def test_get_agent_public_key_calls_correct_endpoint(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, {
            "agent_id": "abc-123", "public_key": "pk==",
        })
        result = client.get_agent_public_key("abc-123")
        call_args = mock_session.request.call_args
        assert call_args[0][0] == "GET"
        assert call_args[0][1].endswith("/v2/agents/abc-123/public-key")
        assert result["public_key"] == "pk=="

    def test_get_agent_public_key_raises_on_404(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(
            404, {"detail": "Agent not found"}
        )
        with pytest.raises(AgentNotFound):
            client.get_agent_public_key("nonexistent-id")


# ===========================================================================
# 23. Send message tests
# ===========================================================================

class TestSendMessage:

    def test_send_message_calls_post(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(201, {
            "status": "sent",
            "message_id": "msg-001",
            "expires_at": "2026-04-01T00:00:00",
        })
        result = client.send_message(
            to_agent_id="agent-456",
            ciphertext="encrypted-data==",
            nonce="nonce-b64==",
            sender_public_key="sender-pk==",
        )
        call_args = mock_session.request.call_args
        assert call_args[0][0] == "POST"
        assert call_args[0][1].endswith("/v2/messages/send")
        body = call_args[1]["json"]
        assert body["to_agent_id"] == "agent-456"
        assert body["ciphertext"] == "encrypted-data=="
        assert body["nonce"] == "nonce-b64=="
        assert body["sender_public_key"] == "sender-pk=="
        assert "payment_id" not in body
        assert result["message_id"] == "msg-001"

    def test_send_message_with_payment_id(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(201, {
            "status": "sent", "message_id": "msg-002", "expires_at": "2026-04-01T00:00:00",
        })
        client.send_message(
            to_agent_id="agent-456",
            ciphertext="ct==",
            nonce="n==",
            sender_public_key="pk==",
            payment_id="pay-123",
        )
        body = mock_session.request.call_args[1]["json"]
        assert body["payment_id"] == "pay-123"

    def test_send_message_sends_auth(self):
        client, mock_session = _build_client(api_key="msg-token")
        mock_session.request.return_value = _make_response(201, {
            "status": "sent", "message_id": "m", "expires_at": "t",
        })
        client.send_message("a", "ct", "n", "pk")
        headers = mock_session.request.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer msg-token"


# ===========================================================================
# 24. Get messages tests
# ===========================================================================

class TestGetMessages:

    def test_get_messages_calls_inbox(self):
        client, mock_session = _build_client()
        inbox = {
            "messages": [{"id": "m1", "ciphertext": "ct=="}],
            "count": 1,
        }
        mock_session.request.return_value = _make_response(200, inbox)
        result = client.get_messages()
        call_args = mock_session.request.call_args
        assert call_args[0][0] == "GET"
        assert call_args[0][1].endswith("/v2/messages/inbox")
        assert result["count"] == 1
        assert len(result["messages"]) == 1

    def test_get_messages_sends_auth(self):
        client, mock_session = _build_client(api_key="inbox-key")
        mock_session.request.return_value = _make_response(200, {"messages": [], "count": 0})
        client.get_messages()
        headers = mock_session.request.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer inbox-key"

    def test_get_messages_empty_inbox(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, {"messages": [], "count": 0})
        result = client.get_messages()
        assert result["count"] == 0
        assert result["messages"] == []


# ===========================================================================
# 25. PoW integration verification tests
# ===========================================================================

class TestPowIntegration:

    def test_solve_pow_challenge_method_exists(self):
        """Verify _solve_pow_challenge is present on the client."""
        client, _ = _build_client()
        assert hasattr(client, "_solve_pow_challenge")
        assert callable(client._solve_pow_challenge)

    def test_solve_pow_returns_dict_on_success(self):
        """PoW solver returns a proof dict when the challenge endpoint works."""
        client, mock_session = _build_client()
        challenge_resp = _make_response(200, {
            "nonce": "test-nonce-abc",
            "difficulty_bits": 1,  # very easy for test speed
            "expires_at": "2026-12-31T00:00:00Z",
        })
        mock_session.request.return_value = challenge_resp
        result = client._solve_pow_challenge()
        assert result is not None
        assert result["nonce"] == "test-nonce-abc"
        assert result["difficulty_bits"] == 1
        assert "solution" in result

    def test_solve_pow_returns_none_on_failure(self):
        """PoW solver returns None when challenge endpoint fails."""
        client, mock_session = _build_client()
        mock_session.request.side_effect = Exception("server down")
        result = client._solve_pow_challenge()
        assert result is None

    def test_auto_register_sends_pow_proof(self):
        """Auto-registration includes pow_challenge when available."""
        client, mock_session = _build_client()
        pow_proof = {
            "nonce": "n", "difficulty_bits": 1,
            "expires_at": "2026-12-31T00:00:00Z", "solution": "42",
        }
        reg_response = _make_response(200, {
            "api_key": "k", "agent_id": "i", "agent_name": "n",
        })
        mock_session.request.return_value = reg_response

        with patch.object(client, "_solve_pow_challenge", return_value=pow_proof), \
             patch("sthrip_sdk.client.save_credentials"), \
             patch("sthrip_sdk.client._generate_agent_name", return_value="n"):
            client._auto_register()

        body = mock_session.request.call_args[1]["json"]
        assert body["pow_challenge"] == pow_proof


# ===========================================================================
# 26. Version tests
# ===========================================================================

class TestVersion:

    def test_version_is_0_3_0(self):
        from sthrip_sdk.client import _VERSION
        assert _VERSION == "0.3.0"

    def test_user_agent_contains_version(self):
        from sthrip_sdk.client import _USER_AGENT
        assert "0.3.0" in _USER_AGENT


# ===========================================================================
# 27. _raw_put tests
# ===========================================================================

class TestRawPut:

    def test_raw_put_sends_put_method(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, {"ok": True})
        client._raw_put("/v2/test", json_body={"key": "value"})
        call_args = mock_session.request.call_args
        assert call_args[0][0] == "PUT"

    def test_raw_put_sends_json_body(self):
        client, mock_session = _build_client()
        mock_session.request.return_value = _make_response(200, {"ok": True})
        client._raw_put("/v2/test", json_body={"key": "value"})
        body = mock_session.request.call_args[1]["json"]
        assert body == {"key": "value"}

    def test_raw_put_sends_auth_by_default(self):
        client, mock_session = _build_client(api_key="put-key")
        mock_session.request.return_value = _make_response(200, {"ok": True})
        client._raw_put("/v2/test")
        headers = mock_session.request.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer put-key"
