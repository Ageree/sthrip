"""Unit tests for Sthrip SDK marketplace v2 methods.

Tests cover:
- sla_template_create() -- POST /v2/sla/templates
- sla_create()          -- POST /v2/sla/contracts
- sla_accept()          -- PATCH /v2/sla/contracts/{id}/accept
- sla_deliver()         -- PATCH /v2/sla/contracts/{id}/deliver
- sla_verify()          -- PATCH /v2/sla/contracts/{id}/verify
- review()              -- POST /v2/agents/{id}/reviews
- matchmake()           -- POST /v2/matchmaking/request
- find_agents() extended -- GET /v2/agents/marketplace with new params

All HTTP calls are intercepted by patching the _raw_* helper methods
directly on the client instance -- no real network traffic.
"""

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: load SDK modules under sthrip_sdk namespace (mirrors test_sdk.py)
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
# SLA template tests
# ===========================================================================

class TestSlaTemplateCreate:

    def test_calls_post_sla_templates_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={"id": "tpl-1"}) as mock:
            client.sla_template_create(
                name="Fast Translation",
                deliverables=["translated_document"],
                response_time_secs=3600,
                delivery_time_secs=86400,
                base_price=0.05,
            )
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/sla/templates"

    def test_payload_contains_required_fields(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={"id": "tpl-1"}) as mock:
            client.sla_template_create(
                name="Fast Translation",
                deliverables=["translated_document"],
                response_time_secs=3600,
                delivery_time_secs=86400,
                base_price=0.05,
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["name"] == "Fast Translation"
        assert payload["deliverables"] == ["translated_document"]
        assert payload["response_time_secs"] == 3600
        assert payload["delivery_time_secs"] == 86400
        assert payload["base_price"] == "0.05"

    def test_base_price_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.sla_template_create(
                name="T",
                deliverables=[],
                response_time_secs=60,
                delivery_time_secs=120,
                base_price=1.25,
            )
        payload = mock.call_args[1]["json_body"]
        assert isinstance(payload["base_price"], str)
        assert payload["base_price"] == "1.25"

    def test_default_penalty_percent_is_10(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.sla_template_create(
                name="T",
                deliverables=[],
                response_time_secs=60,
                delivery_time_secs=120,
                base_price=0.01,
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["penalty_percent"] == 10

    def test_custom_penalty_percent_is_forwarded(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.sla_template_create(
                name="T",
                deliverables=[],
                response_time_secs=60,
                delivery_time_secs=120,
                base_price=0.01,
                penalty_percent=20,
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["penalty_percent"] == 20

    def test_default_service_description_is_empty_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.sla_template_create(
                name="T",
                deliverables=[],
                response_time_secs=60,
                delivery_time_secs=120,
                base_price=0.01,
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["service_description"] == ""

    def test_custom_service_description_is_forwarded(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.sla_template_create(
                name="T",
                deliverables=[],
                response_time_secs=60,
                delivery_time_secs=120,
                base_price=0.01,
                service_description="Translate documents quickly",
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["service_description"] == "Translate documents quickly"

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"id": "tpl-abc", "name": "Fast Translation"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.sla_template_create(
                name="Fast Translation",
                deliverables=[],
                response_time_secs=60,
                delivery_time_secs=120,
                base_price=0.01,
            )
        assert result == expected


# ===========================================================================
# SLA contract create tests
# ===========================================================================

class TestSlaCreate:

    def test_calls_post_sla_contracts_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={"id": "c-1"}) as mock:
            client.sla_create(provider="alice")
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/sla/contracts"

    def test_payload_contains_provider_agent_name(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.sla_create(provider="alice")
        payload = mock.call_args[1]["json_body"]
        assert payload["provider_agent_name"] == "alice"

    def test_template_id_included_when_provided(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.sla_create(provider="alice", template_id="tpl-99")
        payload = mock.call_args[1]["json_body"]
        assert payload["template_id"] == "tpl-99"

    def test_template_id_absent_when_not_provided(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.sla_create(provider="alice")
        payload = mock.call_args[1]["json_body"]
        assert "template_id" not in payload

    def test_price_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.sla_create(provider="alice", price=0.07)
        payload = mock.call_args[1]["json_body"]
        assert payload["price"] == "0.07"

    def test_price_absent_when_not_provided(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.sla_create(provider="alice")
        payload = mock.call_args[1]["json_body"]
        assert "price" not in payload

    def test_extra_kwargs_merged_into_payload(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.sla_create(provider="alice", custom_field="extra_value")
        payload = mock.call_args[1]["json_body"]
        assert payload["custom_field"] == "extra_value"

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"id": "c-xyz", "status": "created"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.sla_create(provider="alice")
        assert result == expected


# ===========================================================================
# SLA accept tests
# ===========================================================================

class TestSlaAccept:

    def test_calls_patch_accept_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_patch", return_value={"status": "accepted"}) as mock:
            client.sla_accept("contract-42")
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/sla/contracts/contract-42/accept"

    def test_contract_id_interpolated_into_path(self):
        client = _build_client()
        with patch.object(client, "_raw_patch", return_value={}) as mock:
            client.sla_accept("abc-123")
        path_arg = mock.call_args[0][0]
        assert "abc-123" in path_arg

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"id": "contract-42", "status": "accepted"}
        with patch.object(client, "_raw_patch", return_value=expected):
            result = client.sla_accept("contract-42")
        assert result == expected


# ===========================================================================
# SLA deliver tests
# ===========================================================================

class TestSlaDeliver:

    def test_calls_patch_deliver_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_patch", return_value={"status": "delivered"}) as mock:
            client.sla_deliver("contract-7")
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/sla/contracts/contract-7/deliver"

    def test_result_hash_included_when_provided(self):
        client = _build_client()
        with patch.object(client, "_raw_patch", return_value={}) as mock:
            client.sla_deliver("contract-7", result_hash="sha256:abc123")
        kwargs = mock.call_args[1]
        assert kwargs["json_body"]["result_hash"] == "sha256:abc123"

    def test_empty_body_when_no_result_hash(self):
        client = _build_client()
        with patch.object(client, "_raw_patch", return_value={}) as mock:
            client.sla_deliver("contract-7")
        kwargs = mock.call_args[1]
        assert kwargs["json_body"] == {}

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"status": "delivered", "delivered_at": "2026-04-01T10:00:00Z"}
        with patch.object(client, "_raw_patch", return_value=expected):
            result = client.sla_deliver("contract-7")
        assert result == expected


# ===========================================================================
# SLA verify tests
# ===========================================================================

class TestSlaVerify:

    def test_calls_patch_verify_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_patch", return_value={"status": "completed"}) as mock:
            client.sla_verify("contract-55")
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/sla/contracts/contract-55/verify"

    def test_contract_id_interpolated_into_path(self):
        client = _build_client()
        with patch.object(client, "_raw_patch", return_value={}) as mock:
            client.sla_verify("unique-id-999")
        path_arg = mock.call_args[0][0]
        assert "unique-id-999" in path_arg

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"id": "contract-55", "status": "completed"}
        with patch.object(client, "_raw_patch", return_value=expected):
            result = client.sla_verify("contract-55")
        assert result == expected


# ===========================================================================
# Review create tests
# ===========================================================================

class TestReviewCreate:

    def test_calls_post_reviews_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={"id": "rev-1"}) as mock:
            client.review(
                agent_id="agent-uuid-1",
                transaction_id="tx-001",
                transaction_type="payment",
                overall_rating=5,
            )
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/agents/agent-uuid-1/reviews"

    def test_agent_id_interpolated_into_path(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.review(
                agent_id="custom-agent-id",
                transaction_id="tx-001",
                transaction_type="payment",
                overall_rating=4,
            )
        path_arg = mock.call_args[0][0]
        assert "custom-agent-id" in path_arg

    def test_payload_contains_required_fields(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.review(
                agent_id="agent-uuid-1",
                transaction_id="tx-001",
                transaction_type="escrow",
                overall_rating=4,
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["transaction_id"] == "tx-001"
        assert payload["transaction_type"] == "escrow"
        assert payload["overall_rating"] == 4

    def test_extra_kwargs_merged_into_payload(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.review(
                agent_id="agent-uuid-1",
                transaction_id="tx-001",
                transaction_type="payment",
                overall_rating=5,
                comment="Excellent service",
                timeliness=5,
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["comment"] == "Excellent service"
        assert payload["timeliness"] == 5

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"id": "rev-abc", "overall_rating": 5}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.review(
                agent_id="agent-uuid-1",
                transaction_id="tx-001",
                transaction_type="payment",
                overall_rating=5,
            )
        assert result == expected


# ===========================================================================
# Matchmaking tests
# ===========================================================================

class TestMatchmake:

    def test_calls_post_matchmaking_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={"request_id": "m-1"}) as mock:
            client.matchmake(
                capabilities=["translation"],
                budget=0.1,
                deadline_secs=3600,
            )
        mock.assert_called_once()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/matchmaking/request"

    def test_payload_contains_required_fields(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.matchmake(
                capabilities=["translation", "proofreading"],
                budget=0.2,
                deadline_secs=7200,
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["required_capabilities"] == ["translation", "proofreading"]
        assert payload["deadline_secs"] == 7200

    def test_budget_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.matchmake(capabilities=[], budget=0.5, deadline_secs=1000)
        payload = mock.call_args[1]["json_body"]
        assert isinstance(payload["budget"], str)
        assert payload["budget"] == "0.5"

    def test_default_min_rating_is_zero_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.matchmake(capabilities=[], budget=0.1, deadline_secs=1000)
        payload = mock.call_args[1]["json_body"]
        assert payload["min_rating"] == "0"

    def test_custom_min_rating_forwarded(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.matchmake(capabilities=[], budget=0.1, deadline_secs=1000, min_rating=4)
        payload = mock.call_args[1]["json_body"]
        assert payload["min_rating"] == "4"

    def test_default_auto_assign_is_false(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.matchmake(capabilities=[], budget=0.1, deadline_secs=1000)
        payload = mock.call_args[1]["json_body"]
        assert payload["auto_assign"] is False

    def test_custom_auto_assign_forwarded(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.matchmake(capabilities=[], budget=0.1, deadline_secs=1000, auto_assign=True)
        payload = mock.call_args[1]["json_body"]
        assert payload["auto_assign"] is True

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"request_id": "m-xyz", "status": "pending"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.matchmake(capabilities=["code-review"], budget=0.3, deadline_secs=3600)
        assert result == expected


# ===========================================================================
# find_agents extended params tests
# ===========================================================================

class TestFindAgentsExtended:

    def test_calls_marketplace_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.find_agents()
        path_arg = mock.call_args[0][0]
        assert path_arg == "/v2/agents/marketplace"

    def test_min_rating_passed_as_query_param(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.find_agents(min_rating=4.0)
        params = mock.call_args[1]["params"]
        assert "min_rating" in params
        assert params["min_rating"] == 4.0

    def test_max_price_passed_as_query_param(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.find_agents(max_price=0.1)
        params = mock.call_args[1]["params"]
        assert "max_price" in params
        assert params["max_price"] == 0.1

    def test_has_sla_passed_as_query_param(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.find_agents(has_sla=True)
        params = mock.call_args[1]["params"]
        assert "has_sla" in params
        assert params["has_sla"] == "true"

    def test_sort_passed_as_query_param(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.find_agents(sort="rating_desc")
        params = mock.call_args[1]["params"]
        assert "sort" in params
        assert params["sort"] == "rating_desc"

    def test_combined_params_all_present(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.find_agents(
                capability="translation",
                min_rating=3.5,
                max_price=0.2,
                has_sla=True,
                sort="price_asc",
            )
        params = mock.call_args[1]["params"]
        assert params["capability"] == "translation"
        assert params["min_rating"] == 3.5
        assert params["max_price"] == 0.2
        assert params["sort"] == "price_asc"

    def test_none_values_excluded_from_params(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.find_agents(min_rating=None, max_price=None)
        params = mock.call_args[1]["params"]
        assert "min_rating" not in params
        assert "max_price" not in params

    def test_returns_list_when_api_returns_list(self):
        client = _build_client()
        agents = [{"agent_id": "a1"}, {"agent_id": "a2"}]
        with patch.object(client, "_raw_get", return_value=agents):
            result = client.find_agents()
        assert result == agents

    def test_returns_agents_from_envelope(self):
        client = _build_client()
        agents = [{"agent_id": "a1"}]
        with patch.object(client, "_raw_get", return_value={"agents": agents, "total": 1}):
            result = client.find_agents()
        assert result == agents

    def test_accepts_escrow_param_forwarded(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.find_agents(accepts_escrow=True)
        params = mock.call_args[1]["params"]
        assert params["accepts_escrow"] == "true"

    def test_uses_unauthenticated_request(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value=[]) as mock:
            client.find_agents()
        kwargs = mock.call_args[1]
        assert kwargs.get("authenticated") is False


# ===========================================================================
# Version bump test
# ===========================================================================

class TestVersionBump:

    def test_version_is_0_4_0(self):
        from sthrip_sdk.client import _VERSION
        assert _VERSION == "0.4.0"
