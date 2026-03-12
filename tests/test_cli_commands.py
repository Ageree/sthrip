import json
import pytest
import httpx
import respx
from typer.testing import CliRunner
from cli.agent_cli.app import app

runner = CliRunner()
BASE_URL = "https://sthrip-api-production.up.railway.app"


# --- register ---

@respx.mock
def test_register_success(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.delenv("STHRIP_API_KEY", raising=False)
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    respx.post(f"{BASE_URL}/v2/agents/register").mock(
        return_value=httpx.Response(201, json={
            "agent_id": "uuid-1", "agent_name": "bot1",
            "api_key": "sk_new", "webhook_secret": "ws_secret", "tier": "free",
        })
    )
    result = runner.invoke(app, ["register", "bot1"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["data"]["api_key"] == "sk_new"
    creds = json.loads((tmp_path / "credentials.json").read_text())
    assert creds["api_key"] == "sk_new"
    assert creds["agent_name"] == "bot1"


@respx.mock
def test_register_with_webhook_url(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.delenv("STHRIP_API_KEY", raising=False)
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    route = respx.post(f"{BASE_URL}/v2/agents/register").mock(
        return_value=httpx.Response(201, json={
            "agent_id": "uuid-1", "agent_name": "bot1",
            "api_key": "sk_new", "webhook_secret": "ws_secret", "tier": "free",
        })
    )
    result = runner.invoke(app, ["register", "bot1", "--webhook-url", "https://example.com/hook"])
    assert result.exit_code == 0
    body = json.loads(route.calls[0].request.content)
    assert body["webhook_url"] == "https://example.com/hook"


@respx.mock
def test_register_api_error(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.delenv("STHRIP_API_KEY", raising=False)
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    respx.post(f"{BASE_URL}/v2/agents/register").mock(
        return_value=httpx.Response(409, json={"detail": "Agent already exists"})
    )
    result = runner.invoke(app, ["register", "bot1"])
    assert result.exit_code == 1
    # Error output goes to stderr (mixed into result.output by CliRunner)
    data = json.loads(result.output)
    assert data["ok"] is False


# --- balance ---

@respx.mock
def test_balance_success(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    respx.get(f"{BASE_URL}/v2/balance").mock(
        return_value=httpx.Response(200, json={"available": "10.0", "pending": "2.0"})
    )
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["available"] == "10.0"


@respx.mock
def test_deposit_success(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    respx.post(f"{BASE_URL}/v2/balance/deposit").mock(
        return_value=httpx.Response(200, json={"deposit_address": "5addr..."})
    )
    result = runner.invoke(app, ["deposit"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "deposit_address" in data["data"]


@respx.mock
def test_withdraw_success(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    respx.post(f"{BASE_URL}/v2/balance/withdraw").mock(
        return_value=httpx.Response(200, json={"tx_id": "tx123", "status": "pending"})
    )
    result = runner.invoke(app, ["withdraw", "5validaddr", "1.5"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["tx_id"] == "tx123"


@respx.mock
def test_deposits_list(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    respx.get(f"{BASE_URL}/v2/balance/deposits").mock(
        return_value=httpx.Response(200, json={"deposits": []})
    )
    result = runner.invoke(app, ["deposits"])
    assert result.exit_code == 0


# --- payments ---

@respx.mock
def test_pay_success(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    route = respx.post(f"{BASE_URL}/v2/payments/hub-routing").mock(
        return_value=httpx.Response(200, json={"payment_id": "p1", "status": "confirmed"})
    )
    result = runner.invoke(app, ["pay", "agent2", "5.0", "--memo", "thx"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["payment_id"] == "p1"
    body = json.loads(route.calls[0].request.content)
    assert body["memo"] == "thx"


@respx.mock
def test_pay_with_idempotency_key(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    route = respx.post(f"{BASE_URL}/v2/payments/hub-routing").mock(
        return_value=httpx.Response(200, json={"payment_id": "p1"})
    )
    runner.invoke(app, ["pay", "agent2", "5.0", "--idempotency-key", "idem1"])
    assert route.calls[0].request.headers["Idempotency-Key"] == "idem1"


@respx.mock
def test_payment_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    respx.get(f"{BASE_URL}/v2/payments/p123").mock(
        return_value=httpx.Response(200, json={"payment_id": "p123", "status": "confirmed"})
    )
    result = runner.invoke(app, ["payment", "p123"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["payment_id"] == "p123"


@respx.mock
def test_history_with_filters(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    route = respx.get(f"{BASE_URL}/v2/payments/history").mock(
        return_value=httpx.Response(200, json={"payments": []})
    )
    result = runner.invoke(app, ["history", "--limit", "5", "--offset", "10", "--direction", "in"])
    assert result.exit_code == 0
    assert route.calls[0].request.url.params["limit"] == "5"
    assert route.calls[0].request.url.params["offset"] == "10"
    assert route.calls[0].request.url.params["direction"] == "in"


# --- agents ---

@respx.mock
def test_agents_list(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    route = respx.get(f"{BASE_URL}/v2/agents").mock(
        return_value=httpx.Response(200, json={"agents": [{"agent_name": "bot1"}]})
    )
    result = runner.invoke(app, ["agents", "list", "--verified", "--limit", "10", "--offset", "5"])
    assert result.exit_code == 0
    assert route.calls[0].request.url.params["verified_only"] == "true"
    assert route.calls[0].request.url.params["limit"] == "10"
    assert route.calls[0].request.url.params["offset"] == "5"


@respx.mock
def test_agents_get(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    respx.get(f"{BASE_URL}/v2/agents/bot1").mock(
        return_value=httpx.Response(200, json={"agent_name": "bot1", "tier": "verified"})
    )
    result = runner.invoke(app, ["agents", "get", "bot1"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["agent_name"] == "bot1"


@respx.mock
def test_leaderboard(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    respx.get(f"{BASE_URL}/v2/leaderboard").mock(
        return_value=httpx.Response(200, json={"agents": []})
    )
    result = runner.invoke(app, ["leaderboard"])
    assert result.exit_code == 0


# --- me ---

@respx.mock
def test_me(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    respx.get(f"{BASE_URL}/v2/me").mock(
        return_value=httpx.Response(200, json={"agent_name": "bot1", "tier": "free"})
    )
    result = runner.invoke(app, ["me"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["agent_name"] == "bot1"


@respx.mock
def test_me_update(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    route = respx.patch(f"{BASE_URL}/v2/me/settings").mock(
        return_value=httpx.Response(200, json={"updated": True})
    )
    result = runner.invoke(app, ["me", "update", "--privacy", "high"])
    assert result.exit_code == 0
    body = json.loads(route.calls[0].request.content)
    assert body["privacy_level"] == "high"


@respx.mock
def test_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    respx.get(f"{BASE_URL}/v2/me/rate-limit").mock(
        return_value=httpx.Response(200, json={"remaining": 100})
    )
    result = runner.invoke(app, ["rate-limit"])
    assert result.exit_code == 0


# --- rotate-key ---

@respx.mock
def test_rotate_key(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_old")
    respx.post(f"{BASE_URL}/v2/me/rotate-key").mock(
        return_value=httpx.Response(200, json={"api_key": "sk_rotated"})
    )
    result = runner.invoke(app, ["rotate-key"])
    assert result.exit_code == 0
    creds = json.loads((tmp_path / "c.json").read_text())
    assert creds["api_key"] == "sk_rotated"


# --- webhooks ---

@respx.mock
def test_webhooks_list(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    respx.get(f"{BASE_URL}/v2/webhooks/events").mock(
        return_value=httpx.Response(200, json={"events": []})
    )
    result = runner.invoke(app, ["webhooks", "list"])
    assert result.exit_code == 0


@respx.mock
def test_webhooks_retry(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.setenv("STHRIP_API_KEY", "sk_test")
    respx.post(f"{BASE_URL}/v2/webhooks/events/evt123/retry").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    result = runner.invoke(app, ["webhooks", "retry", "evt123"])
    assert result.exit_code == 0


# --- diagnostics ---

@respx.mock
def test_health(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    monkeypatch.delenv("STHRIP_API_KEY", raising=False)
    respx.get(f"{BASE_URL}/health").mock(
        return_value=httpx.Response(200, json={"status": "healthy"})
    )
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["status"] == "healthy"


def test_config_show_with_env(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.setenv("STHRIP_API_KEY", "sk_env_key")
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["api_key_source"] == "env"
    assert "sk_env" not in result.stdout


def test_config_show_with_file(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_API_KEY", raising=False)
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    from cli.agent_cli.config import save_config
    save_config({"api_key": "sk_file_key"})
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["api_key_source"] == "file"


def test_config_show_no_auth(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(tmp_path / "c.json"))
    monkeypatch.delenv("STHRIP_API_KEY", raising=False)
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["api_key_source"] == "none"
