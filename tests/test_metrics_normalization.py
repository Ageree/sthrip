"""Tests for Prometheus endpoint path normalization."""


def test_normalize_path_strips_uuids():
    from api.middleware import _normalize_path
    assert _normalize_path("/v2/agents/550e8400-e29b-41d4-a716-446655440000") == "/v2/agents/{id}"


def test_normalize_path_preserves_static():
    from api.middleware import _normalize_path
    assert _normalize_path("/v2/balance") == "/v2/balance"


def test_normalize_path_strips_multiple_dynamic():
    from api.middleware import _normalize_path
    assert _normalize_path("/v2/agents/550e8400-e29b-41d4-a716-446655440000/payments") == "/v2/agents/{id}/payments"


def test_normalize_path_preserves_short_segments():
    from api.middleware import _normalize_path
    assert _normalize_path("/v2/payments/hub-routing") == "/v2/payments/hub-routing"


def test_normalize_path_preserves_hex_like_agent_names():
    """Agent names that look hex-like should NOT be normalized."""
    from api.middleware import _normalize_path
    # Pure alpha hex-like name (no digits) — should be preserved
    assert _normalize_path("/v2/agents/abcdef-fedcba-abcdef-abc") == "/v2/agents/abcdef-fedcba-abcdef-abc"


def test_normalize_path_strips_numeric_hex_id():
    """Long hex IDs with digits should be normalized."""
    from api.middleware import _normalize_path
    assert _normalize_path("/v2/agents/a1b2c3d4e5f6a7b8c9d0e1f2") == "/v2/agents/{id}"
