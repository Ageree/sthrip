"""Tests for startup environment validation."""

import pytest


def test_validate_required_env_uses_settings(monkeypatch):
    """_validate_required_env should use get_settings(), not os.getenv.

    Since database_url has a Pydantic default, removing DATABASE_URL from env
    doesn't make it empty — the default kicks in. This test verifies the function
    uses get_settings() and completes without error when settings are valid.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_API_KEY", "test-key")
    monkeypatch.setenv("ENVIRONMENT", "dev")

    from sthrip.config import get_settings
    get_settings.cache_clear()
    try:
        from api.main_v2 import _validate_required_env
        _validate_required_env()  # database_url has default, should not raise
    finally:
        get_settings.cache_clear()


def test_validate_required_env_fails_without_admin_key(monkeypatch):
    """App must refuse to start without ADMIN_API_KEY."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///test.db")
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "dev")

    from sthrip.config import get_settings
    get_settings.cache_clear()
    try:
        from api.main_v2 import _validate_required_env
        # ADMIN_API_KEY is Field(...) required — pydantic raises ValidationError
        with pytest.raises(Exception):
            _validate_required_env()
    finally:
        get_settings.cache_clear()


def test_validate_required_env_passes_when_set(monkeypatch):
    """No error when required env vars are present."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///test.db")
    monkeypatch.setenv("ADMIN_API_KEY", "test-key")
    monkeypatch.setenv("ENVIRONMENT", "dev")

    from sthrip.config import get_settings
    get_settings.cache_clear()
    try:
        from api.main_v2 import _validate_required_env
        _validate_required_env()  # Should not raise
    finally:
        get_settings.cache_clear()
