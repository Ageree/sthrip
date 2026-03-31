"""
SpendingPolicyService — validates outgoing payments against per-agent
spending policies (max-per-tx, daily limit, session limit, allow/block
lists, escrow threshold).

Redis is used for daily-limit and session-limit tracking via atomic Lua
scripts.  When *redis_client* is ``None`` (tests or environments without
Redis), those two checks are silently skipped.
"""

import fnmatch
import logging
import time
from decimal import Decimal
from typing import Optional

from sthrip.db.models import SpendingPolicy

logger = logging.getLogger("sthrip.spending_policy")


class PolicyViolation(Exception):
    """Raised when a payment violates a spending-policy rule."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


# ---------------------------------------------------------------------------
# Lua scripts — executed atomically inside Redis
# ---------------------------------------------------------------------------

# Daily limit: sorted-set keyed by agent_id, scored by Unix timestamp.
# Each payment is a member.  The script trims entries older than 24 h,
# sums remaining scores, checks whether adding *amount* would exceed the
# limit, and — only on success — records the new member.
_DAILY_LIMIT_LUA = """
local key   = KEYS[1]
local limit = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])
local now   = tonumber(ARGV[3])
local tx_id = ARGV[4]
local window = 86400

-- Remove entries older than 24 h
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

-- Sum existing amounts (stored as member names with score = timestamp,
-- but we encode amount in the member string).  Instead, we use a simpler
-- approach: store amount as the score, timestamp in the member.
-- Actually, let's use a hash for clarity.
-- Simpler: use a sorted set where score = amount and member = tx_id:timestamp.

-- Re-approach: We store each tx as member=tx_id, score=timestamp.
-- We keep a companion key for amount tracking.

-- Simplest correct approach: sorted set with score=timestamp, member=tx_id.
-- Companion hash key:amounts stores tx_id -> amount.

local amounts_key = key .. ':amounts'

-- Trim old entries
local old_members = redis.call('ZRANGEBYSCORE', key, '-inf', now - window)
for _, m in ipairs(old_members) do
    redis.call('HDEL', amounts_key, m)
end
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

-- Sum current amounts
local all_amounts = redis.call('HVALS', amounts_key)
local total = 0
for _, v in ipairs(all_amounts) do
    total = total + tonumber(v)
end

if total + amount > limit then
    return {0, tostring(total)}
end

-- Record the new tx
redis.call('ZADD', key, now, tx_id)
redis.call('HSET', amounts_key, tx_id, tostring(amount))
redis.call('EXPIRE', key, window + 60)
redis.call('EXPIRE', amounts_key, window + 60)

return {1, tostring(total + amount)}
"""

# Session limit: simple key with GET/INCRBYFLOAT + TTL.
# Session keys expire after 1 hour of inactivity.
_SESSION_LIMIT_LUA = """
local key    = KEYS[1]
local limit  = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])
local ttl    = 3600

local current = tonumber(redis.call('GET', key) or '0') or 0

if current + amount > limit then
    return {0, tostring(current)}
end

local new_total = redis.call('INCRBYFLOAT', key, amount)
redis.call('EXPIRE', key, ttl)
return {1, new_total}
"""


class SpendingPolicyService:
    """Stateless service that validates a payment against a SpendingPolicy."""

    def __init__(self, redis_client=None) -> None:
        self._redis = redis_client

    def validate(
        self,
        policy: SpendingPolicy,
        amount: Decimal,
        recipient_name: str,
        session_id: str,
        *,
        is_escrow: bool = False,
        tx_id: Optional[str] = None,
    ) -> None:
        """Run the validation chain.  Raises ``PolicyViolation`` on failure.

        Checks are sequential and fail-fast (first violation wins).
        """
        if not policy.is_active:
            return

        self._check_max_per_tx(policy, amount)
        self._check_allowed_agents(policy, recipient_name)
        self._check_blocked_agents(policy, recipient_name)
        self._check_daily_limit(policy, amount, tx_id)
        self._check_session_limit(policy, amount, session_id)
        self._check_require_escrow_above(policy, amount, is_escrow)

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    @staticmethod
    def _check_max_per_tx(policy: SpendingPolicy, amount: Decimal) -> None:
        if policy.max_per_tx is not None and amount > policy.max_per_tx:
            raise PolicyViolation(
                "max_per_tx",
                f"Amount {amount} exceeds per-transaction limit of {policy.max_per_tx}",
            )

    @staticmethod
    def _check_allowed_agents(policy: SpendingPolicy, recipient_name: str) -> None:
        patterns = policy.allowed_agents
        if not patterns:
            return
        for pattern in patterns:
            if fnmatch.fnmatch(recipient_name, pattern):
                return
        raise PolicyViolation(
            "allowed_agents",
            f"Recipient '{recipient_name}' does not match any allowed pattern",
        )

    @staticmethod
    def _check_blocked_agents(policy: SpendingPolicy, recipient_name: str) -> None:
        patterns = policy.blocked_agents
        if not patterns:
            return
        for pattern in patterns:
            if fnmatch.fnmatch(recipient_name, pattern):
                raise PolicyViolation(
                    "blocked_agents",
                    f"Recipient '{recipient_name}' is blocked by pattern '{pattern}'",
                )

    def _check_daily_limit(
        self,
        policy: SpendingPolicy,
        amount: Decimal,
        tx_id: Optional[str],
    ) -> None:
        if policy.daily_limit is None:
            return
        if self._redis is None:
            return

        key = f"spending:daily:{policy.agent_id}"
        effective_tx_id = tx_id or f"tx:{time.time()}"
        now = time.time()

        try:
            result = self._redis.eval(
                _DAILY_LIMIT_LUA,
                1,
                key,
                str(float(policy.daily_limit)),
                str(float(amount)),
                str(now),
                effective_tx_id,
            )
            allowed = int(result[0])
            if not allowed:
                current_total = result[1]
                raise PolicyViolation(
                    "daily_limit",
                    f"Daily spending would reach {current_total} + {amount}, "
                    f"exceeding limit of {policy.daily_limit}",
                )
        except PolicyViolation:
            raise
        except Exception:
            logger.warning("Redis daily-limit check failed, skipping", exc_info=True)

    def _check_session_limit(
        self,
        policy: SpendingPolicy,
        amount: Decimal,
        session_id: str,
    ) -> None:
        if policy.max_per_session is None:
            return
        if self._redis is None:
            return

        key = f"spending:session:{policy.agent_id}:{session_id}"

        try:
            result = self._redis.eval(
                _SESSION_LIMIT_LUA,
                1,
                key,
                str(float(policy.max_per_session)),
                str(float(amount)),
            )
            allowed = int(result[0])
            if not allowed:
                current_total = result[1]
                raise PolicyViolation(
                    "max_per_session",
                    f"Session spending would reach {current_total} + {amount}, "
                    f"exceeding session limit of {policy.max_per_session}",
                )
        except PolicyViolation:
            raise
        except Exception:
            logger.warning("Redis session-limit check failed, skipping", exc_info=True)

    @staticmethod
    def _check_require_escrow_above(
        policy: SpendingPolicy,
        amount: Decimal,
        is_escrow: bool,
    ) -> None:
        if policy.require_escrow_above is None:
            return
        if amount > policy.require_escrow_above and not is_escrow:
            raise PolicyViolation(
                "require_escrow_above",
                f"Amount {amount} exceeds {policy.require_escrow_above}; "
                f"escrow is required for payments above this threshold",
            )
