"""Phase 3 Sprint 7 — SDK TEE attestation contract tests (8).

Covers:

1. Attestation endpoint payload shape on the TEE service.
2-7. SDK ``verify_tee=True`` happy path, mismatches, signature failure,
     disabled mode, cache TTL, and stale-on-fetch-failure.
8.   Sprint 6 carry-over — dispatcher rejects 4xx with non-dict body.

The TEE side is exercised via ``fastapi.testclient.TestClient``; the SDK
side mocks ``requests`` so no real network is touched.
"""
from __future__ import annotations

import base64
import importlib
import json
import os
import time
from decimal import Decimal
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_keypair() -> tuple[str, str]:
    """Return (priv_b64_seed, pub_b64) Ed25519 keypair."""
    sk = SigningKey.generate()
    priv = base64.b64encode(sk.encode()).decode()
    pub = base64.b64encode(sk.verify_key.encode()).decode()
    return priv, pub


def _sign_attestation(
    quote_b64: str,
    image_hash: str,
    timestamp: str,
    mode: str,
    priv_b64: str,
) -> str:
    """Replicate the attestation_service signing convention."""
    from gcp.payment_tee_deploy.attestation_service import _canonical_for_signing

    payload = {
        "quote_b64": quote_b64,
        "image_hash_sha256": image_hash,
        "timestamp": timestamp,
        "mode": mode,
    }
    sk = SigningKey(base64.b64decode(priv_b64))
    sig = sk.sign(_canonical_for_signing(payload).encode("utf-8")).signature
    return base64.b64encode(sig).decode()


# ---------------------------------------------------------------------------
# Test 1 — TEE attestation endpoint payload shape
# ---------------------------------------------------------------------------


def test_attestation_endpoint_includes_required_fields(monkeypatch):
    """``GET /.well-known/attestation.json`` returns required fields."""
    priv, _pub = _fresh_keypair()
    monkeypatch.setenv("STHRIP_TEE_ATTESTATION_KEY", priv)
    monkeypatch.setenv(
        "STHRIP_TEE_IMAGE_HASH",
        "sha256:abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234",
    )
    # Reload the module so env-driven module state is fresh.
    from gcp.payment_tee_deploy import payment_service  # noqa: F401
    importlib.reload(payment_service)

    client = TestClient(payment_service.app)
    resp = client.get("/.well-known/attestation.json")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    for key in ("quote_b64", "image_hash_sha256", "timestamp", "signature_b64", "mode"):
        assert key in body, f"missing {key} in attestation payload: {body}"

    assert body["mode"] in ("snp_real", "snp_stub")
    assert body["image_hash_sha256"].startswith("sha256:")
    assert isinstance(body["quote_b64"], str) and len(body["quote_b64"]) > 0
    assert isinstance(body["signature_b64"], str) and len(body["signature_b64"]) > 0


# ---------------------------------------------------------------------------
# SDK fixture builders
# ---------------------------------------------------------------------------


def _build_attestation_payload(image_hash: str, priv_b64: str, mode: str = "snp_stub") -> Dict[str, Any]:
    quote_b64 = base64.b64encode(b"stub-attestation-quote").decode()
    timestamp = "2026-05-07T12:00:00+00:00"
    sig_b64 = _sign_attestation(quote_b64, image_hash, timestamp, mode, priv_b64)
    return {
        "quote_b64": quote_b64,
        "image_hash_sha256": image_hash,
        "timestamp": timestamp,
        "signature_b64": sig_b64,
        "mode": mode,
    }


def _make_sthrip(monkeypatch, **kwargs):
    """Construct a Sthrip client with auto-register short-circuited."""
    from sdk.sthrip import client as sdk_client

    monkeypatch.setenv("STHRIP_API_KEY", "sk-test-fake-key-1234567890")
    return sdk_client.Sthrip(api_url="https://sthrip.example.invalid", **kwargs)


# ---------------------------------------------------------------------------
# Test 2 — SDK verify_tee accepts pinned hash
# ---------------------------------------------------------------------------


def test_sdk_verify_tee_accepts_pinned_hash(monkeypatch):
    priv, pub = _fresh_keypair()
    image_hash = "sha256:" + "a" * 64
    payload = _build_attestation_payload(image_hash, priv)

    monkeypatch.setenv("STHRIP_TEE_ATTESTATION_PUBKEY", pub)

    s = _make_sthrip(monkeypatch, verify_tee=True, expected_image_hash=image_hash)

    fake_resp = MagicMock()
    fake_resp.ok = True
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value=payload)

    with patch.object(s._session, "get", return_value=fake_resp):
        # Should not raise.
        s.verify_tee_attestation()


# ---------------------------------------------------------------------------
# Test 3 — mismatching hash → TEEMismatchError
# ---------------------------------------------------------------------------


def test_sdk_verify_tee_rejects_mismatch_hash(monkeypatch):
    from sdk.sthrip.exceptions import TEEMismatchError

    priv, pub = _fresh_keypair()
    image_hash_in_payload = "sha256:" + "a" * 64
    expected_hash = "sha256:" + "b" * 64
    payload = _build_attestation_payload(image_hash_in_payload, priv)

    monkeypatch.setenv("STHRIP_TEE_ATTESTATION_PUBKEY", pub)
    s = _make_sthrip(monkeypatch, verify_tee=True, expected_image_hash=expected_hash)

    fake_resp = MagicMock()
    fake_resp.ok = True
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value=payload)

    with patch.object(s._session, "get", return_value=fake_resp):
        with pytest.raises(TEEMismatchError):
            s.verify_tee_attestation()


# ---------------------------------------------------------------------------
# Test 4 — wrong signing key → TEEMismatchError
# ---------------------------------------------------------------------------


def test_sdk_verify_tee_rejects_invalid_signature(monkeypatch):
    from sdk.sthrip.exceptions import TEEMismatchError

    priv_attacker, _pub_attacker = _fresh_keypair()
    _priv_real, pub_real = _fresh_keypair()
    image_hash = "sha256:" + "c" * 64
    payload = _build_attestation_payload(image_hash, priv_attacker)

    # Pin the REAL pubkey — payload was signed by attacker's key.
    monkeypatch.setenv("STHRIP_TEE_ATTESTATION_PUBKEY", pub_real)
    s = _make_sthrip(monkeypatch, verify_tee=True, expected_image_hash=image_hash)

    fake_resp = MagicMock()
    fake_resp.ok = True
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value=payload)

    with patch.object(s._session, "get", return_value=fake_resp):
        with pytest.raises(TEEMismatchError):
            s.verify_tee_attestation()


# ---------------------------------------------------------------------------
# Test 5 — verify_tee=False is a no-op
# ---------------------------------------------------------------------------


def test_sdk_verify_tee_disabled_skips_check(monkeypatch):
    """Default ``verify_tee=False`` makes ``verify_tee_attestation`` a no-op."""
    s = _make_sthrip(monkeypatch)  # verify_tee not set → False default

    # Even with a guaranteed-broken endpoint, the disabled call returns None.
    fake_get = MagicMock(side_effect=AssertionError("must not be called"))
    with patch.object(s._session, "get", fake_get):
        result = s.verify_tee_attestation()

    assert result is None
    assert fake_get.call_count == 0


# ---------------------------------------------------------------------------
# Test 6 — 5-min cache TTL
# ---------------------------------------------------------------------------


def test_sdk_attestation_cache_5min_ttl(monkeypatch):
    priv, pub = _fresh_keypair()
    image_hash = "sha256:" + "d" * 64
    payload = _build_attestation_payload(image_hash, priv)

    monkeypatch.setenv("STHRIP_TEE_ATTESTATION_PUBKEY", pub)
    s = _make_sthrip(monkeypatch, verify_tee=True, expected_image_hash=image_hash)

    fake_resp = MagicMock()
    fake_resp.ok = True
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value=payload)

    fake_get = MagicMock(return_value=fake_resp)

    # t=0: first call hits network.
    with patch.object(s._session, "get", fake_get):
        s.verify_tee_attestation(now=0.0)
        assert fake_get.call_count == 1

        # t=60s: still cached.
        s.verify_tee_attestation(now=60.0)
        assert fake_get.call_count == 1

        # t=299s: still cached (<5min).
        s.verify_tee_attestation(now=299.0)
        assert fake_get.call_count == 1

        # t=301s: cache expired, refetch.
        s.verify_tee_attestation(now=301.0)
        assert fake_get.call_count == 2


# ---------------------------------------------------------------------------
# Test 7 — fresh fetch failure with no cache → TEEAttestationStaleError
# ---------------------------------------------------------------------------


def test_sdk_attestation_stale_raises_on_fresh_fetch_failure(monkeypatch):
    from sdk.sthrip.exceptions import TEEAttestationStaleError

    _priv, pub = _fresh_keypair()
    monkeypatch.setenv("STHRIP_TEE_ATTESTATION_PUBKEY", pub)
    s = _make_sthrip(monkeypatch, verify_tee=True, expected_image_hash="sha256:" + "e" * 64)

    fake_resp = MagicMock()
    fake_resp.ok = False
    fake_resp.status_code = 503
    fake_resp.text = "service unavailable"
    fake_resp.json = MagicMock(side_effect=ValueError("not JSON"))

    with patch.object(s._session, "get", return_value=fake_resp):
        with pytest.raises(TEEAttestationStaleError):
            s.verify_tee_attestation()


# ---------------------------------------------------------------------------
# Test 8 — Sprint 6 carry-over: 4xx non-dict body → HTTPException, NO fallback
# ---------------------------------------------------------------------------


def test_dispatcher_4xx_non_dict_body_does_not_fallback(monkeypatch):
    """TEE returns 422 with body=``"insufficient balance"`` (plain string).

    Dispatcher must surface the 422 as an HTTPException and NOT fall back
    to the local handler.
    """
    from fastapi import HTTPException

    from sthrip.services import payment_dispatch, payment_tee_client

    monkeypatch.setenv("STHRIP_PAYMENT_VIA_TEE", "true")
    monkeypatch.setenv("STHRIP_TEE_ENDPOINT", "https://tee.example.invalid:8080")

    # Mimic what payment_tee_client returns when the TEE responds 4xx with
    # a non-dict JSON body. The Sprint 6 client preserves the body shape
    # AND tags `_status_code` only for dicts. To exercise the carry-over,
    # we simulate raw client output for non-dict 4xx by making the client
    # raise a TEEUserError with the status code.
    raised = payment_tee_client.TEEUserError(422, "insufficient balance")

    fallback_called = {"v": False}

    def fake_local(*a, **kw):
        fallback_called["v"] = True
        return {"payment_id": "should_not_run"}

    # Build minimal world.
    from decimal import Decimal as _D
    from uuid import uuid4

    class _FakeAgent:
        def __init__(self):
            self.id = uuid4()
            self.tier = MagicMock(value="free")

    class _FakeRecipient:
        def __init__(self):
            self.id = uuid4()
            self.agent_name = "alice"
            self.xmr_address = "4Aexample"

    class _FakeReq:
        urgency = "normal"
        memo = None

    agent = _FakeAgent()
    recipient = _FakeRecipient()

    with patch.object(
        payment_tee_client, "dispatch_hub_routing", side_effect=raised,
    ), patch.object(payment_dispatch, "_local_dispatch", side_effect=fake_local):
        with pytest.raises(HTTPException) as excinfo:
            payment_dispatch.dispatch_hub_routing(
                MagicMock(),
                agent,
                recipient,
                _D("1.0"),
                {"fee_amount": _D("0"), "fee_percent": _D("0"), "total_deduction": _D("1.0")},
                _FakeReq(),
                "test-idem-12345678",
            )

    assert excinfo.value.status_code == 422
    assert "insufficient balance" in str(excinfo.value.detail).lower()
    assert fallback_called["v"] is False
