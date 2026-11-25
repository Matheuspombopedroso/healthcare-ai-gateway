# Part 5 — Rate limits and token quotas in Redis

## The problem

Two different budgets protect the shared backend:

* **requests per minute** — stops a runaway loop in one product from
  saturating the model servers;
* **tokens per day** — caps how much model capacity one product can use.

Both are hot counters touched on every request and they expire on their own,
which is exactly what Redis is good at.

## Rate limit: fixed window

```python
key = f"gw:t{tenant_id}:rpm:{epoch_minute}"
INCR key ; EXPIRE key 120     # in one MULTI/EXEC pipeline
over = count > rpm_limit
```

Trade-off: a fixed window allows up to 2× the limit across a minute boundary.
A sliding window (sorted set or two weighted windows) fixes that at the cost
of more Redis work; for a per-product safety limit, fixed is enough.

## Token quota: reserve, then settle — atomically

The naive version is racy:

```text
used = GET quota        # 50 concurrent requests all read 9,900 / 10,000
if used + est <= limit: # all of them pass
    INCRBY quota est    # quota blown 50x over
```

`gateway/limits.py` runs the check-and-increment as **one Lua script**, which
Redis executes atomically:

```lua
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
if used + amount > limit then return -1 end
local new = redis.call('INCRBY', KEYS[1], amount)
redis.call('EXPIRE', KEYS[1], ttl)
return new
```

The reservation is the worst case (`prompt upper bound + max_tokens`). After
the response, `settle_tokens` adds `actual - reserved` (usually negative), so
the counter ends up holding the real usage. If the request is rejected later
(402) or fails (502), the full reservation is returned.

`tests/test_quotas.py::test_token_quota_is_enforced_and_atomic` fires 20
concurrent requests at a 300-token quota and checks that the final usage is
> 0, ≤ 300, and equal to the prompt+completion tokens recorded in the ledger.

## `max_tokens` is mandatory (with a default)

Every request is given a `max_tokens` (default 256, cap 1024). Without an upper
bound on output length, neither the token reservation nor the credit
reservation (Part 6) could be bounded.

## How to run just this part

```bash
pytest tests/test_quotas.py -v
redis-cli -p 16379 -n 15 keys 'gw:*'    # while tests run, to see the key layout
```

## What to look at in the output

* After a few requests, `GET /v1/usage` shows `tokens_used_today` equal to
  `prompt_tokens + completion_tokens` (reservations were settled to real usage).
* A rejected request returns `{"error": {"type": "rate_limit_error" | "quota_exceeded", ...}}`
  with status 429, and `gateway_rejections_total{reason=...}` increases.
