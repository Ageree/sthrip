"""Integration-flavoured tests for ``railway/op-keystore-deploy/server.py``.

We import the server module directly via importlib (it lives outside the
sthrip package) and exercise its endpoints with FastAPI's ``TestClient``.
The tests skip cleanly if FastAPI is not available in the environment.
"""
from __future__ import annotations

import base64
import importlib.util
import os
import secrets
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

_SERVER_PATH = (
    Path(__file__).resolve().parent.parent
    / "railway"
    / "op-keystore-deploy"
    / "server.py"
)


@pytest.fixture
def server_app(monkeypatch):
    """Boot a fresh server module with a known KEK and AUTH_TOKEN."""
    test_kek = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    test_token = secrets.token_hex(16)
    monkeypatch.setenv("KEK_OP_BASE64", test_kek)
    monkeypatch.setenv("AUTH_TOKEN", test_token)

    spec = importlib.util.spec_from_file_location("op_keystore_server", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, test_token


def test_health_endpoint_no_auth_required(server_app):
    module, _ = server_app
    client = TestClient(module.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_keystore_server_wrap_unwrap_roundtrip(server_app):
    module, token = server_app
    client = TestClient(module.app)

    dek = secrets.token_bytes(32)
    headers = {"Authorization": f"Bearer {token}"}

    wrap_resp = client.post(
        "/wrap",
        json={"dek_b64": base64.b64encode(dek).decode("ascii")},
        headers=headers,
    )
    assert wrap_resp.status_code == 200
    wrapped_b64 = wrap_resp.json()["wrapped_b64"]

    unwrap_resp = client.post(
        "/unwrap",
        json={"wrapped_b64": wrapped_b64},
        headers=headers,
    )
    assert unwrap_resp.status_code == 200
    out = base64.b64decode(unwrap_resp.json()["dek_b64"])
    assert out == dek


def test_keystore_server_requires_auth(server_app):
    module, _ = server_app
    client = TestClient(module.app)
    resp = client.post(
        "/wrap",
        json={"dek_b64": base64.b64encode(secrets.token_bytes(32)).decode("ascii")},
    )
    assert resp.status_code == 401


def test_keystore_server_rejects_wrong_token(server_app):
    module, _ = server_app
    client = TestClient(module.app)
    resp = client.post(
        "/wrap",
        json={"dek_b64": base64.b64encode(secrets.token_bytes(32)).decode("ascii")},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 403


def test_keystore_server_rejects_malformed_auth(server_app):
    module, token = server_app
    client = TestClient(module.app)
    resp = client.post(
        "/wrap",
        json={"dek_b64": base64.b64encode(secrets.token_bytes(32)).decode("ascii")},
        headers={"Authorization": token},  # missing "Bearer " prefix
    )
    assert resp.status_code == 401


def test_keystore_server_rejects_short_dek(server_app):
    module, token = server_app
    client = TestClient(module.app)
    resp = client.post(
        "/wrap",
        json={"dek_b64": base64.b64encode(b"too-short").decode("ascii")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_keystore_server_rejects_short_wrapped(server_app):
    module, token = server_app
    client = TestClient(module.app)
    resp = client.post(
        "/unwrap",
        json={"wrapped_b64": base64.b64encode(b"x").decode("ascii")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_keystore_server_rejects_tampered_wrapped(server_app):
    module, token = server_app
    client = TestClient(module.app)

    dek = secrets.token_bytes(32)
    headers = {"Authorization": f"Bearer {token}"}
    wrap_resp = client.post(
        "/wrap",
        json={"dek_b64": base64.b64encode(dek).decode("ascii")},
        headers=headers,
    )
    wrapped = base64.b64decode(wrap_resp.json()["wrapped_b64"])
    # Flip the last byte to fail the auth tag.
    tampered = wrapped[:-1] + bytes([wrapped[-1] ^ 0x01])
    resp = client.post(
        "/unwrap",
        json={"wrapped_b64": base64.b64encode(tampered).decode("ascii")},
        headers=headers,
    )
    assert resp.status_code == 400
