"""Tests for sthrip_mcp.auth — 3-tier API key loading."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from integrations.sthrip_mcp.auth import (
    AuthError,
    CREDENTIALS_FILE,
    ENV_VAR_NAME,
    load_api_key,
    require_auth,
    save_api_key,
)


class TestLoadApiKey:
    """Test 3-tier API key loading priority."""

    def test_env_var_highest_priority(self, tmp_path):
        """Env var takes priority over credentials file."""
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({"api_key": "sk_from_file"}))

        with patch.dict(os.environ, {ENV_VAR_NAME: "sk_from_env"}), \
             patch("integrations.sthrip_mcp.auth.CREDENTIALS_FILE", creds):
            assert load_api_key() == "sk_from_env"

    def test_file_fallback_when_no_env(self, tmp_path):
        """Falls back to credentials file when env var is not set."""
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({"api_key": "sk_from_file"}))

        env = os.environ.copy()
        env.pop(ENV_VAR_NAME, None)
        with patch.dict(os.environ, env, clear=True), \
             patch("integrations.sthrip_mcp.auth.CREDENTIALS_FILE", creds):
            assert load_api_key() == "sk_from_file"

    def test_none_when_nothing_available(self, tmp_path):
        """Returns None when neither env var nor file exists."""
        missing = tmp_path / "nonexistent.json"

        env = os.environ.copy()
        env.pop(ENV_VAR_NAME, None)
        with patch.dict(os.environ, env, clear=True), \
             patch("integrations.sthrip_mcp.auth.CREDENTIALS_FILE", missing):
            assert load_api_key() is None

    def test_none_when_file_is_invalid_json(self, tmp_path):
        """Returns None when credentials file has invalid JSON."""
        creds = tmp_path / "credentials.json"
        creds.write_text("not json")

        env = os.environ.copy()
        env.pop(ENV_VAR_NAME, None)
        with patch.dict(os.environ, env, clear=True), \
             patch("integrations.sthrip_mcp.auth.CREDENTIALS_FILE", creds):
            assert load_api_key() is None

    def test_none_when_file_missing_api_key_field(self, tmp_path):
        """Returns None when credentials file has no api_key field."""
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({"other": "data"}))

        env = os.environ.copy()
        env.pop(ENV_VAR_NAME, None)
        with patch.dict(os.environ, env, clear=True), \
             patch("integrations.sthrip_mcp.auth.CREDENTIALS_FILE", creds):
            assert load_api_key() is None


class TestSaveApiKey:
    """Test API key persistence."""

    def test_saves_to_file(self, tmp_path):
        creds_dir = tmp_path / ".sthrip"
        creds_file = creds_dir / "credentials.json"

        with patch("integrations.sthrip_mcp.auth.CREDENTIALS_DIR", creds_dir), \
             patch("integrations.sthrip_mcp.auth.CREDENTIALS_FILE", creds_file):
            result_path = save_api_key("sk_test_123")

        assert result_path == creds_file
        data = json.loads(creds_file.read_text())
        assert data["api_key"] == "sk_test_123"

    def test_creates_directory_if_missing(self, tmp_path):
        creds_dir = tmp_path / "deep" / "nested" / ".sthrip"
        creds_file = creds_dir / "credentials.json"

        with patch("integrations.sthrip_mcp.auth.CREDENTIALS_DIR", creds_dir), \
             patch("integrations.sthrip_mcp.auth.CREDENTIALS_FILE", creds_file):
            save_api_key("sk_nested")

        assert creds_file.exists()


class TestRequireAuth:
    """Test auth validation."""

    def test_returns_key_when_present(self):
        assert require_auth("sk_valid") == "sk_valid"

    def test_raises_when_none(self):
        with pytest.raises(AuthError):
            require_auth(None)

    def test_raises_when_empty_string(self):
        with pytest.raises(AuthError):
            require_auth("")
