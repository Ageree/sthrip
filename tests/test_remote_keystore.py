"""Sprint 4b unit tests for ``RemoteKeystore`` (httpx mocked).

These tests exercise only the HTTP client wiring — we never hit a real
sthrip-op-keystore service. The service-side endpoints are exercised in
``tests/test_op_keystore_server.py``.
"""
from __future__ import annotations

import base64
import secrets

import httpx
import pytest

from sthrip.services import operator_keystore as ks_module
from sthrip.services.operator_keystore import (
    RemoteKeystore,
    StubKeystore,
    get_keystore,
)


# ---------------------------------------------------------------------------
# httpx.Client patching helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text or ""

    def json(self) -> dict:
        return dict(self._json)


class _FakeClient:
    """Captures POST calls; returns canned responses keyed by path."""

    def __init__(self, responses: dict[str, _FakeResponse], *, raise_on_post=None):
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}
        self._raise_on_post = raise_on_post

    def post(self, url: str, json: dict | None = None):  # noqa: A002 — match httpx api
        self.calls.append((url, dict(json or {})))
        if self._raise_on_post is not None:
            raise self._raise_on_post
        # Match by path suffix
        for suffix, resp in self._responses.items():
            if url.endswith(suffix):
                return resp
        raise AssertionError(f"_FakeClient: no canned response for {url}")


@pytest.fixture
def fake_remote(monkeypatch):
    """Build a RemoteKeystore with httpx.Client patched to record calls."""

    def _build(responses: dict[str, _FakeResponse], *, token="bearer-secret",
               url="http://op-keystore.test:8000", raise_on_post=None):
        monkeypatch.setenv("OP_KEYSTORE_AUTH_TOKEN", token)
        monkeypatch.setenv("OP_KEYSTORE_URL", url)

        fake = _FakeClient(responses, raise_on_post=raise_on_post)

        # Patch httpx.Client to return our fake regardless of constructor args.
        def _fake_client_ctor(*args, **kwargs):
            fake.headers.update(dict(kwargs.get("headers") or {}))
            return fake

        monkeypatch.setattr(httpx, "Client", _fake_client_ctor)
        get_keystore.cache_clear()
        return RemoteKeystore(), fake

    return _build


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_remote_keystore_requires_auth_token(monkeypatch):
    monkeypatch.delenv("OP_KEYSTORE_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="OP_KEYSTORE_AUTH_TOKEN"):
        RemoteKeystore()


def test_remote_auth_header_set(fake_remote):
    rk, fake = fake_remote(
        responses={"/wrap": _FakeResponse(200, {"wrapped_b64": "AA=="})},
        token="my-bearer-secret",
    )
    # Headers captured at constructor time.
    assert fake.headers.get("Authorization") == "Bearer my-bearer-secret"


def test_remote_url_default(monkeypatch):
    monkeypatch.setenv("OP_KEYSTORE_AUTH_TOKEN", "tok")
    monkeypatch.delenv("OP_KEYSTORE_URL", raising=False)

    fake = _FakeClient({})
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: fake)
    rk = RemoteKeystore()
    assert rk._url == "http://sthrip-op-keystore.railway.internal:8000"


# ---------------------------------------------------------------------------
# wrap / unwrap
# ---------------------------------------------------------------------------


def test_remote_unwrap_calls_endpoint(fake_remote):
    plaintext_dek = secrets.token_bytes(32)
    encoded_dek = base64.b64encode(plaintext_dek).decode()
    rk, fake = fake_remote(
        responses={"/unwrap": _FakeResponse(200, {"dek_b64": encoded_dek})},
    )

    wrapped = b"\x01" * 64  # arbitrary 64-byte blob (>= 28 = nonce+tag minimum)
    out = rk.unwrap_dek(wrapped)
    assert out == plaintext_dek

    assert len(fake.calls) == 1
    url, body = fake.calls[0]
    assert url == "http://op-keystore.test:8000/unwrap"
    assert body == {"wrapped_b64": base64.b64encode(wrapped).decode("ascii")}


def test_remote_wrap_calls_endpoint(fake_remote):
    expected_wrapped = b"\x02" * 64
    rk, fake = fake_remote(
        responses={
            "/wrap": _FakeResponse(
                200, {"wrapped_b64": base64.b64encode(expected_wrapped).decode("ascii")}
            ),
        },
    )
    dek = secrets.token_bytes(32)
    out = rk.wrap_dek(dek)
    assert out == expected_wrapped

    url, body = fake.calls[0]
    assert url.endswith("/wrap")
    assert body == {"dek_b64": base64.b64encode(dek).decode("ascii")}


def test_remote_wrap_unwrap_roundtrip(fake_remote):
    """The fake server roundtrips by mirroring base64 — useful for checking
    that wrap+unwrap chain through the client without losing bytes."""
    dek = secrets.token_bytes(32)
    wrapped_blob = b"\x09" * 64
    rk, fake = fake_remote(
        responses={
            "/wrap": _FakeResponse(
                200, {"wrapped_b64": base64.b64encode(wrapped_blob).decode("ascii")}
            ),
            "/unwrap": _FakeResponse(
                200, {"dek_b64": base64.b64encode(dek).decode("ascii")}
            ),
        },
    )
    wrapped = rk.wrap_dek(dek)
    assert wrapped == wrapped_blob
    out = rk.unwrap_dek(wrapped)
    assert out == dek


def test_remote_unwrap_error_handling_non_200(fake_remote):
    rk, _ = fake_remote(
        responses={"/unwrap": _FakeResponse(403, text="forbidden")},
    )
    with pytest.raises(RuntimeError, match="HTTP 403"):
        rk.unwrap_dek(b"\x00" * 64)


def test_remote_unwrap_error_on_connection_refused(fake_remote):
    rk, _ = fake_remote(
        responses={},
        raise_on_post=httpx.ConnectError("connection refused"),
    )
    with pytest.raises(RuntimeError, match="unreachable"):
        rk.unwrap_dek(b"\x00" * 64)


def test_remote_wrap_rejects_wrong_dek_length(fake_remote):
    rk, _ = fake_remote(responses={})
    with pytest.raises(ValueError, match="32 bytes"):
        rk.wrap_dek(b"too-short")


def test_remote_unwrap_rejects_short_blob(fake_remote):
    rk, _ = fake_remote(responses={})
    with pytest.raises(ValueError, match="too short"):
        rk.unwrap_dek(b"x" * 5)


def test_remote_unwrap_rejects_bad_response_payload(fake_remote):
    rk, _ = fake_remote(
        responses={"/unwrap": _FakeResponse(200, {"unexpected_field": "hi"})},
    )
    with pytest.raises(RuntimeError, match="missing dek_b64"):
        rk.unwrap_dek(b"\x00" * 64)


def test_remote_unwrap_rejects_wrong_dek_length_response(fake_remote):
    rk, _ = fake_remote(
        responses={
            "/unwrap": _FakeResponse(
                200, {"dek_b64": base64.b64encode(b"\x00" * 16).decode("ascii")}
            ),
        },
    )
    with pytest.raises(RuntimeError, match="wrong length"):
        rk.unwrap_dek(b"\x00" * 64)


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------


def test_get_keystore_returns_remote_when_mode_remote(monkeypatch):
    monkeypatch.setenv("OP_KEYSTORE_MODE", "remote")
    monkeypatch.setenv("OP_KEYSTORE_AUTH_TOKEN", "tok")

    fake = _FakeClient({})
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: fake)
    get_keystore.cache_clear()

    ks = get_keystore()
    assert isinstance(ks, RemoteKeystore)


def test_get_keystore_returns_stub_by_default(monkeypatch):
    monkeypatch.delenv("OP_KEYSTORE_MODE", raising=False)
    get_keystore.cache_clear()
    ks = get_keystore()
    assert isinstance(ks, StubKeystore)


def test_get_keystore_returns_stub_when_explicit(monkeypatch):
    monkeypatch.setenv("OP_KEYSTORE_MODE", "stub")
    get_keystore.cache_clear()
    ks = get_keystore()
    assert isinstance(ks, StubKeystore)


def test_remote_get_kek_for_envelope_unsupported(monkeypatch):
    monkeypatch.setenv("OP_KEYSTORE_AUTH_TOKEN", "tok")

    fake = _FakeClient({})
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: fake)
    rk = RemoteKeystore()
    with pytest.raises(RuntimeError, match="get_kek_for_envelope"):
        rk.get_kek_for_envelope()
