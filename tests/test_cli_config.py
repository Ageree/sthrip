import json
import os
import stat
import pytest
from cli.agent_cli.config import (
    DEFAULT_BASE_URL,
    CREDENTIALS_PATH,
    load_config,
    save_config,
    resolve_api_key,
    resolve_base_url,
)


@pytest.fixture
def creds_file(tmp_path, monkeypatch):
    """Redirect credentials to a temp file."""
    path = tmp_path / "credentials.json"
    monkeypatch.setattr("cli.agent_cli.config.CREDENTIALS_PATH", str(path))
    return path


def test_default_base_url_is_production():
    assert "sthrip-api-production" in DEFAULT_BASE_URL


def test_load_config_returns_empty_dict_when_no_file(creds_file):
    assert load_config() == {}


def test_save_config_creates_file_with_0600(creds_file):
    save_config({"api_key": "sk_test123"})
    mode = stat.S_IMODE(os.stat(str(creds_file)).st_mode)
    assert mode == 0o600


def test_save_config_merges_fields(creds_file):
    save_config({"api_key": "sk_first"})
    save_config({"agent_name": "bot1"})
    data = json.loads(creds_file.read_text())
    assert data["api_key"] == "sk_first"
    assert data["agent_name"] == "bot1"


def test_save_config_overwrites_existing_field(creds_file):
    save_config({"api_key": "sk_old"})
    save_config({"api_key": "sk_new"})
    data = json.loads(creds_file.read_text())
    assert data["api_key"] == "sk_new"


def test_resolve_api_key_env_takes_priority(creds_file, monkeypatch):
    save_config({"api_key": "sk_file"})
    monkeypatch.setenv("STHRIP_API_KEY", "sk_env")
    assert resolve_api_key() == "sk_env"


def test_resolve_api_key_falls_back_to_file(creds_file, monkeypatch):
    monkeypatch.delenv("STHRIP_API_KEY", raising=False)
    save_config({"api_key": "sk_file"})
    assert resolve_api_key() == "sk_file"


def test_resolve_api_key_returns_none_when_nothing(creds_file, monkeypatch):
    monkeypatch.delenv("STHRIP_API_KEY", raising=False)
    assert resolve_api_key() is None


def test_resolve_base_url_flag_takes_priority(creds_file, monkeypatch):
    monkeypatch.setenv("STHRIP_BASE_URL", "http://env")
    save_config({"base_url": "http://file"})
    assert resolve_base_url(flag_url="http://flag") == "http://flag"


def test_resolve_base_url_env_over_file(creds_file, monkeypatch):
    monkeypatch.setenv("STHRIP_BASE_URL", "http://env")
    save_config({"base_url": "http://file"})
    assert resolve_base_url() == "http://env"


def test_resolve_base_url_file_over_default(creds_file, monkeypatch):
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    save_config({"base_url": "http://file"})
    assert resolve_base_url() == "http://file"


def test_resolve_base_url_falls_back_to_default(creds_file, monkeypatch):
    monkeypatch.delenv("STHRIP_BASE_URL", raising=False)
    assert resolve_base_url() == DEFAULT_BASE_URL
