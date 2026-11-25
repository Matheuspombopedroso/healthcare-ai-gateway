"""Per-tenant rate limits and token quotas in Redis.

Two controls, both keyed by tenant id so tenants can never consume each
other's budget:

* **Rate limit** -- requests per minute, a fixed window counter
  (``INCR`` + ``EXPIRE``). Simple and cheap; the known trade-off is that a
  burst at a window boundary can briefly reach 2x the limit. A sliding window
  would fix that at the cost of more Redis work per request.

* **Daily token quota** -- reserve-then-settle. Before calling the model we
  *reserve* the worst-case token count (prompt estimate + ``max_tokens``) with
  an atomic Lua script that refuses the reservation if it would exceed the
  quota. After the response we *settle* by adding ``actual - reserved`` (a
  negative number, usually). A naive "read usage, compare, then INCRBY" is
  racy: 50 concurrent requests could all read "under quota" and all pass.

Key layout (everything under the tenant's namespace)::

    gw:t{tenant_id}:rpm:{epoch_minute}
    gw:t{tenant_id}:tokens:{YYYYMMDD}
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from redis.asyncio import Redis

# Atomically: if used + amount > limit, reject (return -1) without changing
# anything; otherwise add and return the new total. Running as a Lua script
# guarantees no other command interleaves between the read and the write.
_RESERVE_LUA = """
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
local amount = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
if used + amount > limit then
  return -1
end
local new = redis.call('INCRBY', KEYS[1], amount)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return new
"""

_DAY_TTL_SECONDS = 2 * 24 * 3600  # keep a day key a bit longer than a day


def rpm_key(tenant_id: int, now: float | None = None) -> str:
    """Redis key for the tenant's current one-minute window."""
    minute = int((now if now is not None else time.time()) // 60)
    return f"gw:t{tenant_id}:rpm:{minute}"


def tokens_key(tenant_id: int, now: datetime | None = None) -> str:
    """Redis key for the tenant's token usage on the current UTC day."""
    day = (now or datetime.now(UTC)).strftime("%Y%m%d")
    return f"gw:t{tenant_id}:tokens:{day}"


class Limiter:
    """Thin wrapper over Redis implementing the two tenant controls."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self._reserve = redis.register_script(_RESERVE_LUA)

    async def hit_rate_limit(self, tenant_id: int, limit: int) -> bool:
        """Count one request; return ``True`` if the tenant is now over its RPM.

        The rejected request is still counted, which is the usual behaviour
        (a client hammering the gateway keeps itself throttled).
        """
        key = rpm_key(tenant_id)
        pipe = self.redis.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, 120)
        count, _ = await pipe.execute()
        return int(count) > limit

    async def reserve_tokens(self, tenant_id: int, amount: int, quota: int) -> str | None:
        """Reserve ``amount`` tokens of today's quota.

        Returns the Redis key to settle against later, or ``None`` if the
        reservation would exceed the quota (the request must be rejected).
        """
        key = tokens_key(tenant_id)
        result = await self._reserve(keys=[key], args=[amount, quota, _DAY_TTL_SECONDS])
        return None if int(result) == -1 else key

    async def settle_tokens(self, key: str, reserved: int, actual: int) -> None:
        """Replace a reservation with the actual usage (``actual - reserved``)."""
        delta = actual - reserved
        if delta:
            await self.redis.incrby(key, delta)

    async def tokens_used_today(self, tenant_id: int) -> int:
        """Current token usage for the tenant (for the ``/v1/usage`` endpoint)."""
        value = await self.redis.get(tokens_key(tenant_id))
        return int(value or 0)
