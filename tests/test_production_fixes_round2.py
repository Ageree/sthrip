"""
TDD tests for production readiness fixes — Round 2.

Covers remaining CRITICAL and HIGH issues from code review:
1.  Hub payment cross-session atomicity (fee_collector)
2.  Admin auth rate-limit TOCTOU
3.  Audit log fires before DB commit
4.  AdminSessionStore._ensure_redis thread safety
5.  synchronize_session missing on bulk UPDATE
6.  Request body size limit bypass on DELETE
7.  _run_database_migrations "already exists" too broad
8.  Rate limiter failed auth TOCTOU
9.  IP counter charged when global limit exceeded
10. _transfer_to_payment direction misclassification
11. Naive datetime in _transfer_to_payment
12. StealthAddress cache unbounded growth
"""

import inspect
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Hub payment: fee_collector must not open independent session for hub routes
# ═══════════════════════════════════════════════════════════════════════════════


class TestHubPaymentAtomicity:
    """create_hub_route must NOT open its own get_db() when db is passed."""

    def test_create_hub_route_uses_caller_session_when_provided(self):
        """When db= is passed, create_hub_route must use that session, not get_db()."""
        source = inspect.getsource(
            __import__("sthrip.services.fee_collector", fromlist=["FeeCollector"]).FeeCollector.create_hub_route
        )
        # The method must check `if db is not None` and use the caller's session
        assert "if db is not None" in source or "db is not None" in source, (
            "create_hub_route must branch on db parameter"
        )

    def test_settle_hub_route_accepts_db_parameter(self):
        """settle_hub_route must accept an optional db parameter."""
        sig = inspect.signature(
            __import__("sthrip.services.fee_collector", fromlist=["FeeCollector"]).FeeCollector.settle_hub_route
        )
        assert "db" in sig.parameters, (
            "settle_hub_route must accept a db= parameter for transactional use"
        )

    def test_hub_payment_all_in_one_session(self):
        """_execute_hub_transfer must pass db to all fee_collector methods."""
        source = inspect.getsource(
            __import__("api.routers.payments", fromlist=["_execute_hub_transfer"])._execute_hub_transfer
        )
        # Must pass db= to create_hub_route AND confirm_hub_route
        assert "db=db" in source or "db = db" in source, (
            "_execute_hub_transfer must pass caller's db session to fee_collector"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Admin auth rate-limit: must use atomic increment-then-check
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdminAuthRateLimit:
    """Admin auth must not use separate check_only + increment (TOCTOU)."""

    def test_admin_auth_no_check_only_toctou(self):
        """admin_auth must not use check_only=True pattern (TOCTOU vulnerable)."""
        source = inspect.getsource(
            __import__("api.routers.admin", fromlist=["admin_auth"]).admin_auth
        )
        assert "check_only=True" not in source, (
            "admin_auth must not use check_only=True — TOCTOU vulnerable. "
            "Use atomic increment-then-check instead."
        )

    def test_admin_ui_login_no_check_only_toctou(self):
        """admin UI login_submit must not use check_only=True pattern."""
        with open("api/admin_ui/views.py") as f:
            source = f.read()
        # Find login_submit function
        func_start = source.index("async def login_submit")
        func_end = source.find("\nasync def ", func_start + 1)
        if func_end == -1:
            func_end = source.find("\n@router.", func_start + 1)
        if func_end == -1:
            func_end = len(source)
        func_body = source[func_start:func_end]
        assert "check_only=True" not in func_body, (
            "login_submit must not use check_only=True — TOCTOU vulnerable"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Audit log must use caller's DB session (atomic with commit)
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuditLogAtomicity:
    """audit_log in update_agent_settings and rotate_api_key must use db session."""

    def test_update_settings_passes_db_to_audit(self):
        """update_agent_settings must pass db= to audit_log."""
        source = inspect.getsource(
            __import__("api.routers.agents", fromlist=["update_agent_settings"]).update_agent_settings
        )
        assert "audit_log(" in source, (
            "update_agent_settings must call audit_log"
        )
        # db=db must appear in the function body (multi-line call)
        assert "db=db" in source, (
            "audit_log call in update_agent_settings must include db= parameter"
        )

    def test_rotate_key_passes_db_to_audit(self):
        """rotate_api_key must pass db= to audit_log."""
        source = inspect.getsource(
            __import__("api.routers.agents", fromlist=["rotate_api_key"]).rotate_api_key
        )
        assert "audit_log(" in source, (
            "rotate_api_key must call audit_log"
        )
        # db=db must appear in the function body (multi-line call)
        assert "db=db" in source, (
            "audit_log call in rotate_api_key must include db= parameter"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. AdminSessionStore._ensure_redis must be thread-safe
# ═══════════════════════════════════════════════════════════════════════════════


class TestSessionStoreThreadSafety:
    """_ensure_redis must use a lock to prevent TOCTOU race."""

    def test_ensure_redis_uses_lock(self):
        """_ensure_redis must use threading.Lock for initialization."""
        source = inspect.getsource(
            __import__("api.session_store", fromlist=["AdminSessionStore"]).AdminSessionStore._ensure_redis
        )
        # Must use a lock
        assert "lock" in source.lower() or "Lock" in source, (
            "_ensure_redis must use a threading.Lock to prevent TOCTOU race"
        )

    def test_session_store_has_lock_attribute(self):
        """AdminSessionStore must have a _init_lock attribute."""
        cls = __import__("api.session_store", fromlist=["AdminSessionStore"]).AdminSessionStore
        store = cls()
        assert hasattr(store, "_init_lock") or hasattr(cls, "_init_lock"), (
            "AdminSessionStore must have a _init_lock for thread-safe init"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 5. synchronize_session on bulk UPDATE
# ═══════════════════════════════════════════════════════════════════════════════


class TestSynchronizeSession:
    """Bulk .update() calls must specify synchronize_session."""

    def test_update_last_seen_has_synchronize_session(self):
        """update_last_seen must pass synchronize_session to .update()."""
        source = inspect.getsource(
            __import__("sthrip.db.agent_repo", fromlist=["AgentRepository"]).AgentRepository.update_last_seen
        )
        assert "synchronize_session" in source, (
            "update_last_seen .update() must specify synchronize_session"
        )

    def test_update_wallet_addresses_has_synchronize_session(self):
        """update_wallet_addresses must pass synchronize_session to .update()."""
        source = inspect.getsource(
            __import__("sthrip.db.agent_repo", fromlist=["AgentRepository"]).AgentRepository.update_wallet_addresses
        )
        assert "synchronize_session" in source, (
            "update_wallet_addresses .update() must specify synchronize_session"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Request body size limit: must include DELETE
# ═══════════════════════════════════════════════════════════════════════════════


class TestBodySizeLimit:
    """Body size check must cover DELETE requests too."""

    def test_body_limit_covers_delete(self):
        """Chunked body check must include DELETE method."""
        with open("api/middleware.py") as f:
            source = f.read()
        # Find the method check line for chunked body enforcement
        assert '"DELETE"' in source or "'DELETE'" in source, (
            "Body size limit for chunked transfers must include DELETE method"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Migration error: must check pgcode, not string match
# ═══════════════════════════════════════════════════════════════════════════════


class TestMigrationErrorHandling:
    """Migration error handler must use specific error code, not broad string match."""

    def test_migration_uses_specific_error_check(self):
        """_run_database_migrations must check error code or narrow string."""
        source = inspect.getsource(
            __import__("api.main_v2", fromlist=["_run_database_migrations"])._run_database_migrations
        )
        # Must either check pgcode or use a more specific string pattern
        has_pgcode = "pgcode" in source or "pg_code" in source
        has_specific_code = "42P07" in source
        has_narrow_match = "relation" in source and "already exists" in source
        assert has_pgcode or has_specific_code or has_narrow_match, (
            "Migration error handler must use pgcode (42P07) or narrow string match, "
            "not broad 'already exists'"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Rate limiter: record_failed_auth must be atomic
# ═══════════════════════════════════════════════════════════════════════════════


class TestFailedAuthAtomicity:
    """record_failed_auth must use atomic check+increment, not separate pipeline."""

    def test_record_failed_auth_returns_count(self):
        """record_failed_auth must return current count for caller to check limit."""
        sig = inspect.signature(
            __import__("sthrip.services.rate_limiter", fromlist=["RateLimiter"]).RateLimiter.record_failed_auth
        )
        # The method should have been refactored to check-and-increment atomically
        source = inspect.getsource(
            __import__("sthrip.services.rate_limiter", fromlist=["RateLimiter"]).RateLimiter.record_failed_auth
        )
        # Must use Lua script for atomicity OR return the count
        has_lua = "eval" in source or "_RATE_LIMIT_LUA" in source or "_FAILED_AUTH_LUA" in source
        returns_int = "return" in source and ("count" in source or "int(" in source)
        assert has_lua or returns_int, (
            "record_failed_auth must use atomic Lua script or return count for TOCTOU safety"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 9. IP counter: global check must come before IP increment
# ═══════════════════════════════════════════════════════════════════════════════


class TestIPCounterOrdering:
    """Global rate limit must be checked before IP counter is incremented."""

    def test_check_ip_redis_checks_global_first(self):
        """_check_ip_redis must check global limit before incrementing IP counter."""
        source = inspect.getsource(
            __import__("sthrip.services.rate_limiter", fromlist=["RateLimiter"]).RateLimiter._check_ip_redis
        )
        # Global check must come before IP increment, OR use a single combined Lua script
        has_combined_lua = "global" in source.split("ip_result")[0] if "ip_result" in source else False
        has_single_script = source.count("eval") == 1
        # Or: check global with peek first, then increment IP
        has_global_peek_first = "global" in source[:source.index("ip")] if "ip" in source and "global" in source else False
        # Most practical: use single Lua for both
        assert has_combined_lua or has_single_script or has_global_peek_first or "global_key" in source[:200], (
            "_check_ip_redis must check global limit before incrementing IP, "
            "or use a single combined Lua script for both"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 10. _transfer_to_payment direction classification
# ═══════════════════════════════════════════════════════════════════════════════


class TestTransferDirection:
    """_transfer_to_payment must use transfer type field, not negative amount."""

    def test_uses_type_field_not_negative_amount(self):
        """Direction must be determined from transfer['type'], not amount < 0."""
        source = inspect.getsource(
            __import__("sthrip.client", fromlist=["Sthrip"]).Sthrip._transfer_to_payment
        )
        # Must NOT use `amount_atomic < 0` for direction
        assert "amount_atomic < 0" not in source, (
            "Direction must use transfer type field, not negative amount heuristic"
        )
        # Must use the type field
        assert 'type' in source or '"out"' in source or "'out'" in source, (
            "Must check transfer['type'] for direction classification"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Naive datetime fix
# ═══════════════════════════════════════════════════════════════════════════════


class TestTransferTimezone:
    """_transfer_to_payment must use timezone-aware datetime."""

    def test_fromtimestamp_uses_utc(self):
        """datetime.fromtimestamp must pass tz=timezone.utc."""
        source = inspect.getsource(
            __import__("sthrip.client", fromlist=["Sthrip"]).Sthrip._transfer_to_payment
        )
        # Must not have bare fromtimestamp without tz
        if "fromtimestamp" in source:
            assert "tz=" in source or "timezone.utc" in source, (
                "datetime.fromtimestamp must include tz=timezone.utc"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 12. StealthAddress cache bounds
# ═══════════════════════════════════════════════════════════════════════════════


class TestStealthCacheBounds:
    """StealthAddressManager._cache must have a maximum size."""

    def test_cache_has_max_size(self):
        """_cache must be bounded (LRU or max-size eviction)."""
        source = inspect.getsource(
            __import__("sthrip.stealth", fromlist=["StealthAddressManager"]).StealthAddressManager
        )
        has_max_size = "MAX_CACHE" in source or "max_cache" in source or "maxsize" in source
        has_lru = "lru" in source.lower() or "OrderedDict" in source
        has_eviction = "evict" in source.lower() or "pop" in source or "del " in source
        assert has_max_size or has_lru or has_eviction, (
            "StealthAddressManager._cache must have bounded size with eviction"
        )
