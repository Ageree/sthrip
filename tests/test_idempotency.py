"""Tests for sthrip/services/idempotency.py — idempotency key store."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

import sthrip.services.idempotency as idempotency_mod
from sthrip.services.idempotency import (
    IdempotencyStore,
    _PROCESSING_SENTINEL,
    _TTL_SECONDS,
    get_idempotency_store,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_local_store() -> IdempotencyStore:
    """Create a store that always uses local fallback (no Redis)."""
    orig = idempotency_mod.REDIS_AVAILABLE
    try:
        idempotency_mod.REDIS_AVAILABLE = False
        store = IdempotencyStore()
    finally:
        idempotency_mod.REDIS_AVAILABLE = orig
    assert store.use_redis is False
    return store


def _make_redis_store() -> IdempotencyStore:
    """Create a store with a mocked Redis client."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True

    orig = idempotency_mod.REDIS_AVAILABLE
    try:
        idempotency_mod.REDIS_AVAILABLE = True
        with patch.object(idempotency_mod, "redis") as mock_mod:
            mock_mod.from_url.return_value = mock_redis
            store = IdempotencyStore()
    finally:
        idempotency_mod.REDIS_AVAILABLE = orig

    assert store.use_redis is True
    assert store.redis is mock_redis
    return store


# ── IdempotencyStore initialization ───────────────────────────────────────


class TestInit:
    def test_local_fallback_when_redis_unavailable(self):
        store = _make_local_store()
        assert store.redis is None
        assert store.use_redis is False

    def test_redis_mode_when_available(self):
        store = _make_redis_store()
        assert store.use_redis is True

    def test_falls_back_when_redis_ping_fails(self):
        mock_redis = MagicMock()
        mock_redis.ping.side_effect = Exception("connection refused")

        orig = idempotency_mod.REDIS_AVAILABLE
        try:
            idempotency_mod.REDIS_AVAILABLE = True
            with patch.object(idempotency_mod, "redis") as mock_mod:
                mock_mod.from_url.return_value = mock_redis
                store = IdempotencyStore()
        finally:
            idempotency_mod.REDIS_AVAILABLE = orig

        assert store.use_redis is False


# ── Key generation ────────────────────────────────────────────────────────


class TestKeyGeneration:
    def test_key_format(self):
        store = _make_local_store()
        assert store._key("agent1", "/pay", "k1") == "idempotency:agent1:/pay:k1"

    def test_different_agents_dont_collide(self):
        store = _make_local_store()
        assert store._key("a", "/pay", "k") != store._key("b", "/pay", "k")

    def test_different_endpoints_dont_collide(self):
        store = _make_local_store()
        assert store._key("a", "/pay", "k") != store._key("a", "/refund", "k")

    def test_empty_strings(self):
        store = _make_local_store()
        assert store._key("", "", "") == "idempotency:::"

    def test_long_key(self):
        store = _make_local_store()
        long = "x" * 500
        key = store._key(long, long, long)
        assert long in key


# ── Local mode: try_reserve / store_response / release ────────────────────


class TestLocalReserve:
    def test_first_call_returns_none(self):
        store = _make_local_store()
        assert store.try_reserve("a", "/pay", "k1") is None

    def test_second_call_while_processing_raises_409(self):
        store = _make_local_store()
        store.try_reserve("a", "/pay", "k1")
        with pytest.raises(HTTPException) as exc_info:
            store.try_reserve("a", "/pay", "k1")
        assert exc_info.value.status_code == 409

    def test_returns_cached_after_store(self):
        store = _make_local_store()
        store.try_reserve("a", "/pay", "k1")
        response = {"status": "ok", "amount": 100}
        store.store_response("a", "/pay", "k1", response)
        cached = store.try_reserve("a", "/pay", "k1")
        assert cached == response

    def test_release_clears_reservation(self):
        store = _make_local_store()
        store.try_reserve("a", "/pay", "k1")
        store.release("a", "/pay", "k1")
        # After release, a new reserve should succeed (return None)
        assert store.try_reserve("a", "/pay", "k1") is None

    def test_release_noop_when_already_stored(self):
        store = _make_local_store()
        store.try_reserve("a", "/pay", "k1")
        store.store_response("a", "/pay", "k1", {"ok": True})
        # Release should NOT delete a stored response (only sentinel)
        store.release("a", "/pay", "k1")
        cached = store.try_reserve("a", "/pay", "k1")
        assert cached == {"ok": True}

    def test_expired_entry_treated_as_new(self):
        store = _make_local_store()
        store.try_reserve("a", "/pay", "k1")
        store.store_response("a", "/pay", "k1", {"ok": True})
        # Manually expire the entry
        full_key = store._key("a", "/pay", "k1")
        store._local_cache[full_key]["expires_at"] = time.time() - 1
        # Should treat as new
        assert store.try_reserve("a", "/pay", "k1") is None

    def test_different_agent_keys_independent(self):
        store = _make_local_store()
        store.try_reserve("agent-a", "/pay", "k1")
        # Different agent can reserve the same endpoint+key
        assert store.try_reserve("agent-b", "/pay", "k1") is None


# ── Redis mode: try_reserve ───────────────────────────────────────────────


class TestRedisReserve:
    def test_first_call_returns_none(self):
        store = _make_redis_store()
        store.redis.set.return_value = True  # SET NX succeeded
        assert store.try_reserve("a", "/pay", "k1") is None

    def test_cached_response_returned(self):
        store = _make_redis_store()
        store.redis.set.return_value = False  # NX failed
        store.redis.get.return_value = json.dumps({"status": "ok"})
        result = store.try_reserve("a", "/pay", "k1")
        assert result == {"status": "ok"}

    def test_processing_sentinel_raises_409(self):
        store = _make_redis_store()
        store.redis.set.return_value = False
        store.redis.get.return_value = _PROCESSING_SENTINEL
        with pytest.raises(HTTPException) as exc_info:
            store.try_reserve("a", "/pay", "k1")
        assert exc_info.value.status_code == 409

    def test_key_expired_between_set_and_get_retries(self):
        store = _make_redis_store()
        # First SET NX fails, GET returns None (expired), retry SET NX succeeds
        store.redis.set.side_effect = [False, True]
        store.redis.get.return_value = None
        assert store.try_reserve("a", "/pay", "k1") is None

    def test_key_expired_retry_also_fails(self):
        store = _make_redis_store()
        store.redis.set.side_effect = [False, False]
        store.redis.get.return_value = None
        with pytest.raises(HTTPException) as exc_info:
            store.try_reserve("a", "/pay", "k1")
        assert exc_info.value.status_code == 409


# ── Redis mode: store_response ────────────────────────────────────────────


class TestRedisStoreResponse:
    def test_stores_json(self):
        store = _make_redis_store()
        store.store_response("a", "/pay", "k1", {"amount": 42})
        call_args = store.redis.set.call_args
        assert json.loads(call_args[0][1]) == {"amount": 42}
        assert call_args[1]["ex"] == _TTL_SECONDS

    def test_redis_write_failure_logged(self):
        store = _make_redis_store()
        store.redis.set.side_effect = Exception("write fail")
        # Should not raise, just log
        store.store_response("a", "/pay", "k1", {"ok": True})


# ── Redis mode: release ──────────────────────────────────────────────────


class TestRedisRelease:
    def test_calls_eval(self):
        store = _make_redis_store()
        store.release("a", "/pay", "k1")
        store.redis.eval.assert_called_once()

    def test_redis_release_failure_logged(self):
        store = _make_redis_store()
        store.redis.eval.side_effect = Exception("eval fail")
        # Should not raise
        store.release("a", "/pay", "k1")


# ── Legacy API: get_cached_response ───────────────────────────────────────


class TestLegacyGetCached:
    def test_local_returns_cached(self):
        store = _make_local_store()
        store.try_reserve("a", "/pay", "k1")
        store.store_response("a", "/pay", "k1", {"legacy": True})
        assert store.get_cached_response("k1", "/pay", agent_id="a") == {"legacy": True}

    def test_local_returns_none_when_empty(self):
        store = _make_local_store()
        assert store.get_cached_response("k1", "/pay") is None

    def test_local_returns_none_when_expired(self):
        store = _make_local_store()
        store.try_reserve("a", "/pay", "k1")
        store.store_response("a", "/pay", "k1", {"ok": True})
        full_key = store._key("a", "/pay", "k1")
        store._local_cache[full_key]["expires_at"] = time.time() - 1
        assert store.get_cached_response("k1", "/pay", agent_id="a") is None

    def test_redis_returns_cached(self):
        store = _make_redis_store()
        store.redis.get.return_value = json.dumps({"redis": True})
        assert store.get_cached_response("k1", "/pay", agent_id="a") == {"redis": True}

    def test_redis_ignores_sentinel(self):
        store = _make_redis_store()
        store.redis.get.return_value = _PROCESSING_SENTINEL
        assert store.get_cached_response("k1", "/pay") is None

    def test_redis_read_failure(self):
        store = _make_redis_store()
        store.redis.get.side_effect = Exception("read fail")
        assert store.get_cached_response("k1", "/pay") is None


# ── get_idempotency_store singleton ───────────────────────────────────────


class TestGetIdempotencyStore:
    def test_returns_store(self):
        idempotency_mod._store = None
        orig = idempotency_mod.REDIS_AVAILABLE
        try:
            idempotency_mod.REDIS_AVAILABLE = False
            store = get_idempotency_store()
            assert isinstance(store, IdempotencyStore)
        finally:
            idempotency_mod.REDIS_AVAILABLE = orig
            idempotency_mod._store = None
