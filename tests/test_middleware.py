"""Tests for API middleware (security headers, body size, etc.)."""

import os
import pytest
from unittest.mock import patch

os.environ.setdefault("MONERO_NETWORK", "stagenet")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("HUB_MODE", "ledger")


@pytest.fixture
def client():
    """Create a test client with middleware active."""
    from fastapi.testclient import TestClient
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    with patch("api.main_v2.lifespan", noop_lifespan):
        import importlib
        import api.main_v2
        importlib.reload(api.main_v2)
        app = api.main_v2.create_app()
        yield TestClient(app)


def test_oversized_body_rejected_with_content_length_header(client):
    """Request with Content-Length > 1MB must be rejected."""
    resp = client.post(
        "/v2/agents/register",
        content=b"x" * 100,
        headers={"Content-Length": str(2 * 1024 * 1024), "Content-Type": "application/json"},
    )
    assert resp.status_code == 413


def test_oversized_body_rejected_by_body_read(client):
    """Body size enforcement must also read actual body for POST requests."""
    # This tests the body-reading path (not just Content-Length header check)
    large_body = b"x" * (2 * 1024 * 1024)
    resp = client.post(
        "/v2/agents/register",
        content=large_body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413


def test_normal_body_accepted(client):
    """Small body requests should pass through body size check."""
    resp = client.get("/health")
    assert resp.status_code != 413


def test_responses_include_csp_header(client):
    """All responses must include Content-Security-Policy."""
    resp = client.get("/health")
    assert "content-security-policy" in resp.headers
