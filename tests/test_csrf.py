"""Tests for CSRF token on admin login."""

import os
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


ADMIN_KEY = "test-admin-key-for-tests-long-enough-32"


@pytest.fixture
def client():
    with patch("api.main_v2.lifespan") as mock_lifespan:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def noop_lifespan(app):
            yield
        mock_lifespan.side_effect = noop_lifespan

        # Re-import to get fresh app
        import importlib
        import api.main_v2
        importlib.reload(api.main_v2)
        yield TestClient(api.main_v2.create_app())


def test_login_form_contains_csrf_token(client):
    """GET /admin/login must include a CSRF token in the form."""
    resp = client.get("/admin/login")
    assert resp.status_code == 200
    assert "csrf_token" in resp.text


def test_login_rejects_missing_csrf(client):
    """POST /admin/login without CSRF token must fail."""
    resp = client.post("/admin/login", data={"admin_key": "test-key"})
    assert resp.status_code == 403


def test_login_rejects_invalid_csrf(client):
    """POST /admin/login with wrong CSRF token must fail."""
    resp = client.post("/admin/login", data={"admin_key": "test-key", "csrf_token": "wrong"})
    assert resp.status_code == 403


def test_login_accepts_valid_csrf(client):
    """POST /admin/login with valid CSRF + correct key succeeds."""
    # Get CSRF token from login page
    resp = client.get("/admin/login")
    import re
    match = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert match, "No csrf_token found in form"
    csrf_token = match.group(1)

    resp = client.post(
        "/admin/login",
        data={"admin_key": ADMIN_KEY, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303  # Redirect to /admin/
