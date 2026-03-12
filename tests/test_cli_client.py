import json
import pytest
import httpx
import respx
from cli.agent_cli.client import StrhipClient, CliError
from cli.agent_cli.output import (
    EXIT_API_ERROR, EXIT_AUTH_ERROR, EXIT_NETWORK_ERROR,
)


@pytest.fixture
def client():
    return StrhipClient(
        base_url="https://test.example.com",
        api_key="sk_test123",
        timeout=5,
        debug=False,
    )


@respx.mock
def test_get_success(client):
    respx.get("https://test.example.com/v2/balance").mock(
        return_value=httpx.Response(200, json={"available": "10.0"})
    )
    result = client.get("/v2/balance")
    assert result == {"available": "10.0"}


@respx.mock
def test_post_success(client):
    respx.post("https://test.example.com/v2/agents/register").mock(
        return_value=httpx.Response(201, json={"agent_id": "abc"})
    )
    result = client.post("/v2/agents/register", json={"agent_name": "bot1"})
    assert result == {"agent_id": "abc"}


@respx.mock
def test_post_with_idempotency_key(client):
    route = respx.post("https://test.example.com/v2/payments/hub-routing").mock(
        return_value=httpx.Response(200, json={"payment_id": "p1"})
    )
    client.post(
        "/v2/payments/hub-routing",
        json={"to_agent_name": "bot2", "amount": "1.0"},
        idempotency_key="key123",
    )
    assert route.calls[0].request.headers["Idempotency-Key"] == "key123"


@respx.mock
def test_auth_header_sent(client):
    route = respx.get("https://test.example.com/v2/me").mock(
        return_value=httpx.Response(200, json={"agent_name": "bot1"})
    )
    client.get("/v2/me")
    assert route.calls[0].request.headers["Authorization"] == "Bearer sk_test123"


@respx.mock
def test_401_raises_auth_error(client):
    respx.get("https://test.example.com/v2/me").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid token"})
    )
    with pytest.raises(CliError) as exc_info:
        client.get("/v2/me")
    assert exc_info.value.exit_code == EXIT_AUTH_ERROR


@respx.mock
def test_403_raises_auth_error(client):
    respx.get("https://test.example.com/v2/me").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"})
    )
    with pytest.raises(CliError) as exc_info:
        client.get("/v2/me")
    assert exc_info.value.exit_code == EXIT_AUTH_ERROR


@respx.mock
def test_404_raises_api_error(client):
    respx.get("https://test.example.com/v2/agents/unknown").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )
    with pytest.raises(CliError) as exc_info:
        client.get("/v2/agents/unknown")
    assert exc_info.value.exit_code == EXIT_API_ERROR


@respx.mock
def test_500_raises_api_error(client):
    respx.get("https://test.example.com/health").mock(
        return_value=httpx.Response(500, json={"detail": "Internal error"})
    )
    with pytest.raises(CliError) as exc_info:
        client.get("/health")
    assert exc_info.value.exit_code == EXIT_API_ERROR


def test_network_error_raises_cli_error(client):
    with respx.mock:
        respx.get("https://test.example.com/health").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        with pytest.raises(CliError) as exc_info:
            client.get("/health")
        assert exc_info.value.exit_code == EXIT_NETWORK_ERROR


def test_timeout_raises_network_error(client):
    with respx.mock:
        respx.get("https://test.example.com/health").mock(
            side_effect=httpx.TimeoutException("Timed out")
        )
        with pytest.raises(CliError) as exc_info:
            client.get("/health")
        assert exc_info.value.exit_code == EXIT_NETWORK_ERROR


@respx.mock
def test_no_auth_header_when_no_api_key():
    client = StrhipClient(
        base_url="https://test.example.com",
        api_key=None,
        timeout=5,
        debug=False,
    )
    route = respx.post("https://test.example.com/v2/agents/register").mock(
        return_value=httpx.Response(201, json={"agent_id": "abc"})
    )
    client.post("/v2/agents/register", json={"agent_name": "bot1"})
    assert "Authorization" not in route.calls[0].request.headers


@respx.mock
def test_patch_method(client):
    respx.patch("https://test.example.com/v2/me/settings").mock(
        return_value=httpx.Response(200, json={"updated": True})
    )
    result = client.patch("/v2/me/settings", json={"privacy_level": "high"})
    assert result == {"updated": True}
