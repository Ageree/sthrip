"""Unit tests for the unified AdminSessionStore.

TDD RED phase: these tests are written before the implementation exists.
They fully specify the contract of api.session_store.AdminSessionStore
and the get_session_store() singleton factory.

Coverage targets:
- create_session / validate_session / delete_session
- set_session (dashboard-style: caller provides token + TTL)
- get_session (dashboard-style: returns bool)
- create_csrf_token / verify_csrf_token
- in-memory eviction on capacity overflow
- configurable key prefix
- Redis backend (mocked)
- edge cases: empty token, invalid token, expired entries
"""

import hashlib
import time
import pytest
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(key_prefix: str = "admin_session:", redis=None):
    """Construct a fresh AdminSessionStore, optionally injecting a Redis mock.

    Always bypasses the lazy Redis-connection attempt so tests are hermetic:
    - pass redis=<mock> to exercise the Redis code path
    - pass nothing (default) to exercise the in-memory fallback
    """
    from api.session_store import AdminSessionStore
    store = AdminSessionStore(key_prefix=key_prefix)
    # Disable auto-connection so tests never hit a real Redis instance.
    store._redis_checked = True
    if redis is not None:
        store._redis = redis
    return store


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# create_session / validate_session (API-style interface)
# ---------------------------------------------------------------------------

class TestCreateSession:
    def test_create_session_returns_token(self):
        """create_session() must return a non-empty urlsafe string token."""
        store = _make_store()
        token = store.create_session(ttl=3600)
        assert isinstance(token, str)
        assert len(token) >= 20

    def test_create_session_different_tokens_each_call(self):
        """Each create_session() call returns a unique token."""
        store = _make_store()
        tokens = {store.create_session(ttl=3600) for _ in range(10)}
        assert len(tokens) == 10

    def test_create_session_stores_hash_not_plaintext(self):
        """The plaintext token must NOT appear in the in-memory store (only its hash)."""
        store = _make_store()
        token = store.create_session(ttl=3600)
        assert token not in store._local
        assert _token_hash(token) in store._local


class TestValidateSession:
    def test_validate_session_valid_token(self):
        """validate_session() returns True for a freshly created token."""
        store = _make_store()
        token = store.create_session(ttl=3600)
        assert store.validate_session(token) is True

    def test_validate_session_expired_token(self):
        """validate_session() returns False for a token whose TTL has elapsed."""
        store = _make_store()
        token = store.create_session(ttl=1)
        # Force expiry by reaching into the internal store
        token_hash = _token_hash(token)
        store._local[token_hash] = {"expires": time.time() - 1}
        assert store.validate_session(token) is False

    def test_validate_session_expired_entry_removed(self):
        """Accessing an expired token must remove it from the local dict."""
        store = _make_store()
        token = store.create_session(ttl=1)
        token_hash = _token_hash(token)
        store._local[token_hash] = {"expires": time.time() - 1}
        store.validate_session(token)
        assert token_hash not in store._local

    def test_validate_session_invalid_token(self):
        """validate_session() returns False for a token that was never created."""
        store = _make_store()
        assert store.validate_session("completely-made-up-token") is False

    def test_validate_session_empty_string(self):
        """validate_session() returns False for an empty string."""
        store = _make_store()
        assert store.validate_session("") is False


class TestDeleteSession:
    def test_delete_session(self):
        """delete_session() removes a previously created token."""
        store = _make_store()
        token = store.create_session(ttl=3600)
        assert store.validate_session(token) is True
        store.delete_session(token)
        assert store.validate_session(token) is False

    def test_delete_session_nonexistent_is_noop(self):
        """delete_session() on an unknown token does not raise."""
        store = _make_store()
        store.delete_session("token-that-does-not-exist")  # must not raise


# ---------------------------------------------------------------------------
# set_session / get_session (dashboard-style interface)
# ---------------------------------------------------------------------------

class TestSetGetSession:
    def test_set_session_then_get_session_returns_true(self):
        """set_session(token, ttl) followed by get_session(token) returns True."""
        store = _make_store()
        token = "dashboard-token-abc123"
        store.set_session(token, ttl=3600)
        assert store.get_session(token) is True

    def test_get_session_unknown_token_returns_false(self):
        """get_session() returns False for a token that was never set."""
        store = _make_store()
        assert store.get_session("not-set") is False

    def test_set_session_expired_get_returns_false(self):
        """get_session() returns False when the stored entry is past its TTL."""
        store = _make_store()
        token = "expiry-token"
        store.set_session(token, ttl=1)
        token_hash = _token_hash(token)
        store._local[token_hash] = {"expires": time.time() - 1}
        assert store.get_session(token) is False

    def test_set_session_overwrite(self):
        """Calling set_session twice with the same token updates the entry."""
        store = _make_store()
        token = "overwrite-token"
        store.set_session(token, ttl=10)
        # Force it to expire
        token_hash = _token_hash(token)
        store._local[token_hash] = {"expires": time.time() - 1}
        # Re-set it with fresh TTL
        store.set_session(token, ttl=3600)
        assert store.get_session(token) is True


# ---------------------------------------------------------------------------
# CSRF tokens
# ---------------------------------------------------------------------------

class TestCreateCsrfToken:
    def test_create_csrf_token_returns_nonempty_string(self):
        """create_csrf_token() returns a urlsafe string."""
        store = _make_store()
        token = store.create_csrf_token()
        assert isinstance(token, str)
        assert len(token) >= 20

    def test_create_csrf_token_unique_each_call(self):
        """Each create_csrf_token() call returns a different token."""
        store = _make_store()
        tokens = {store.create_csrf_token() for _ in range(10)}
        assert len(tokens) == 10

    def test_create_csrf_token_stored_under_csrf_prefix(self):
        """The CSRF token is stored under a 'csrf:' prefixed key (no session prefix)."""
        store = _make_store(key_prefix="admin_session:")
        token = store.create_csrf_token()
        key = f"csrf:{_token_hash(token)}"
        assert key in store._local


class TestVerifyCsrfToken:
    def test_verify_csrf_token_valid(self):
        """verify_csrf_token() returns True for a freshly created CSRF token."""
        store = _make_store()
        token = store.create_csrf_token()
        assert store.verify_csrf_token(token) is True

    def test_verify_csrf_token_consumed(self):
        """A CSRF token is single-use: second verify returns False."""
        store = _make_store()
        token = store.create_csrf_token()
        assert store.verify_csrf_token(token) is True
        assert store.verify_csrf_token(token) is False

    def test_verify_csrf_token_consumed_removed_from_store(self):
        """After consumption, the CSRF key is absent from the local store."""
        store = _make_store()
        token = store.create_csrf_token()
        store.verify_csrf_token(token)
        key = f"csrf:{_token_hash(token)}"
        assert key not in store._local

    def test_verify_csrf_token_empty(self):
        """verify_csrf_token('') returns False immediately."""
        store = _make_store()
        assert store.verify_csrf_token("") is False

    def test_verify_csrf_token_none_like_empty(self):
        """verify_csrf_token with a whitespace-only string returns False."""
        store = _make_store()
        assert store.verify_csrf_token("   ") is False

    def test_verify_csrf_token_invalid(self):
        """verify_csrf_token() returns False for a token never created."""
        store = _make_store()
        assert store.verify_csrf_token("not-a-real-csrf-token") is False

    def test_verify_csrf_token_expired(self):
        """verify_csrf_token() returns False for an expired CSRF entry."""
        store = _make_store()
        token = store.create_csrf_token()
        key = f"csrf:{_token_hash(token)}"
        store._local[key] = {"expires": time.time() - 1}
        assert store.verify_csrf_token(token) is False


# ---------------------------------------------------------------------------
# In-memory eviction
# ---------------------------------------------------------------------------

class TestEviction:
    def test_evict_expired_entries(self):
        """_evict_expired() removes entries whose 'expires' is in the past."""
        store = _make_store()
        now = time.time()
        store._local["expired-key"] = {"expires": now - 1}
        store._local["fresh-key"] = {"expires": now + 3600}
        store._evict_expired()
        assert "expired-key" not in store._local
        assert "fresh-key" in store._local

    def test_evict_expired_leaves_non_dict_entries(self):
        """_evict_expired() does not choke on non-dict values (defensive)."""
        store = _make_store()
        store._local["weird-key"] = "string-value"
        store._evict_expired()  # must not raise
        assert "weird-key" in store._local

    def test_max_local_entries_triggers_eviction(self):
        """When _local reaches MAX_LOCAL_ENTRIES, create_session evicts before adding."""
        from api.session_store import AdminSessionStore
        store = AdminSessionStore(key_prefix="admin_session:")
        store._redis_checked = True  # disable Redis init

        # Fill the store with expired entries up to the limit
        now = time.time()
        for i in range(store._MAX_LOCAL_ENTRIES):
            store._local[f"fill-{i}"] = {"expires": now - 1}

        assert len(store._local) == store._MAX_LOCAL_ENTRIES

        # create_session should trigger eviction so size stays under control
        store.create_session(ttl=3600)

        # All the expired fill entries must have been removed
        assert len(store._local) < store._MAX_LOCAL_ENTRIES


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------

class TestRedisBackend:
    def _redis_mock(self):
        mock = MagicMock()
        mock.ping.return_value = True
        return mock

    def test_create_session_uses_setex(self):
        """When Redis is available, create_session stores via SETEX."""
        redis = self._redis_mock()
        store = _make_store(redis=redis)
        token = store.create_session(ttl=3600)
        token_hash = _token_hash(token)
        redis.setex.assert_called_once_with(
            f"admin_session:{token_hash}", 3600, "1"
        )

    def test_validate_session_calls_get(self):
        """validate_session hits Redis GET and returns True when key exists."""
        redis = self._redis_mock()
        redis.get.return_value = "1"
        store = _make_store(redis=redis)
        token = "some-token"
        result = store.validate_session(token)
        token_hash = _token_hash(token)
        redis.get.assert_called_once_with(f"admin_session:{token_hash}")
        assert result is True

    def test_validate_session_redis_miss_returns_false(self):
        """validate_session returns False when Redis GET returns None."""
        redis = self._redis_mock()
        redis.get.return_value = None
        store = _make_store(redis=redis)
        assert store.validate_session("no-such-token") is False

    def test_delete_session_calls_redis_delete(self):
        """delete_session calls Redis DELETE with the correct hashed key."""
        redis = self._redis_mock()
        store = _make_store(redis=redis)
        token = "token-to-delete"
        store.delete_session(token)
        token_hash = _token_hash(token)
        redis.delete.assert_called_once_with(f"admin_session:{token_hash}")

    def test_set_session_uses_setex(self):
        """set_session stores the token via Redis SETEX."""
        redis = self._redis_mock()
        store = _make_store(redis=redis)
        token = "dashboard-cookie-token"
        store.set_session(token, ttl=28800)
        token_hash = _token_hash(token)
        redis.setex.assert_called_once_with(
            f"admin_session:{token_hash}", 28800, "1"
        )

    def test_get_session_delegates_to_redis(self):
        """get_session uses Redis GET when Redis is available."""
        redis = self._redis_mock()
        redis.get.return_value = "1"
        store = _make_store(redis=redis)
        assert store.get_session("any-token") is True

    def test_create_csrf_uses_setex(self):
        """create_csrf_token stores under 'csrf:' prefix in Redis."""
        redis = self._redis_mock()
        store = _make_store(redis=redis)
        token = store.create_csrf_token()
        token_hash = _token_hash(token)
        redis.setex.assert_called_once_with(f"csrf:{token_hash}", 600, "1")

    def test_verify_csrf_redis_hit_deletes_key(self):
        """verify_csrf_token consumes the key atomically via Redis pipeline."""
        redis = self._redis_mock()
        pipe_mock = MagicMock()
        pipe_mock.execute.return_value = ["1", 1]  # get result, delete result
        redis.pipeline.return_value = pipe_mock
        store = _make_store(redis=redis)
        token = "csrf-token"
        result = store.verify_csrf_token(token)
        token_hash = _token_hash(token)
        redis.pipeline.assert_called_once_with(True)
        pipe_mock.get.assert_called_once_with(f"csrf:{token_hash}")
        pipe_mock.delete.assert_called_once_with(f"csrf:{token_hash}")
        pipe_mock.execute.assert_called_once()
        assert result is True

    def test_verify_csrf_redis_miss_returns_false(self):
        """verify_csrf_token returns False when Redis returns None for CSRF key."""
        redis = self._redis_mock()
        pipe_mock = MagicMock()
        pipe_mock.execute.return_value = [None, 0]
        redis.pipeline.return_value = pipe_mock
        store = _make_store(redis=redis)
        assert store.verify_csrf_token("missing-csrf") is False

    def test_custom_key_prefix_in_redis(self):
        """Key prefix is applied correctly to Redis keys."""
        redis = self._redis_mock()
        store = _make_store(key_prefix="admin_api_session:", redis=redis)
        token = store.create_session(ttl=100)
        token_hash = _token_hash(token)
        redis.setex.assert_called_once_with(
            f"admin_api_session:{token_hash}", 100, "1"
        )


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

class TestEnsureRedisAutoConnect:
    """Tests for the lazy _ensure_redis() connection paths."""

    def test_ensure_redis_success_sets_redis_attribute(self):
        """When Redis ping succeeds, _redis is populated."""
        from api.session_store import AdminSessionStore
        store = AdminSessionStore(key_prefix="admin_session:")

        mock_client = MagicMock()
        mock_client.ping.return_value = True

        with patch("api.session_store.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url="redis://localhost/0")
            with patch("redis.from_url", return_value=mock_client):
                store._ensure_redis()

        assert store._redis is mock_client
        assert store._redis_checked is True

    def test_ensure_redis_failure_leaves_redis_none(self):
        """When Redis connection raises, _redis stays None (in-memory fallback)."""
        from api.session_store import AdminSessionStore
        store = AdminSessionStore(key_prefix="admin_session:")

        with patch("api.session_store.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url="redis://badhost/0")
            with patch("redis.from_url", side_effect=Exception("connection refused")):
                store._ensure_redis()

        assert store._redis is None
        assert store._redis_checked is True

    def test_ensure_redis_idempotent(self):
        """Calling _ensure_redis() twice only runs the connection logic once."""
        from api.session_store import AdminSessionStore
        store = AdminSessionStore(key_prefix="admin_session:")

        with patch("api.session_store.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url=None)
            store._ensure_redis()
            store._ensure_redis()
            # get_settings should be called exactly once
            assert mock_settings.call_count == 1


class TestGetSessionStoreSingleton:
    def test_get_session_store_returns_instance(self):
        """get_session_store() returns an AdminSessionStore instance."""
        from api.session_store import get_session_store, AdminSessionStore
        store = get_session_store()
        assert isinstance(store, AdminSessionStore)

    def test_get_session_store_returns_same_object(self):
        """get_session_store() returns the same singleton on repeated calls."""
        from api.session_store import get_session_store
        assert get_session_store() is get_session_store()
