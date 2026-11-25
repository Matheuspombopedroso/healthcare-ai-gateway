# Part 4 — Tenants, API keys and isolation

## The problem

Three products share one gateway. Product A must never be able to read
product B's usage, spend B's credits, or exhaust B's limits — not through a
bug, and not through a crafted request.

## How it works

**1. Identity comes only from the key.** `gateway/auth.py` hashes the
presented Bearer key (SHA-256) and looks up the tenant by hash. No endpoint
accepts a tenant id or name from the client; every later step receives the
`Tenant` object built here.

```python
digest = hash_key(api_key)
row = select(tenants).where(tenants.c.api_key_hash == digest)
```

Only hashes are stored in Postgres, so a database dump does not leak keys.
Keys themselves come from environment variables (`config/tenants.yaml` only
names the variable).

**2. Every data access is scoped by `tenant.id`.**

* Postgres: `usage_summary(tenant_id)`, `balance(tenant_id)`, `reserve(tenant_id, …)`.
* Redis: keys are namespaced `gw:t{tenant_id}:rpm:…` and `gw:t{tenant_id}:tokens:…`.

**3. Per-tenant model allowlist.** `tenant.allowed_models` is checked before
anything else; unknown models get the *same* 403 as forbidden ones, so a
tenant cannot discover which models other tenants use.

**4. Admin operations use a separate secret.** Top-ups require
`X-Admin-Token`; a tenant key is rejected there.

## The proof: `tests/test_isolation.py`

| Test | What it proves |
|---|---|
| `test_missing_or_bad_key_is_rejected` | no key / wrong key → 401 |
| `test_model_allowlist_is_per_tenant` | beta cannot use alpha's premium model |
| `test_usage_and_credits_only_show_own_tenant` | B sees zero of A's usage; `?tenant=alpha` is ignored |
| `test_one_tenant_exhausting_budget_does_not_affect_another` | A flooding to 402 while B runs concurrently: all B requests succeed and B pays exactly for its own |
| `test_rate_limit_of_one_tenant_does_not_throttle_another` | A's 429s don't affect B |
| `test_redis_keys_are_namespaced_by_tenant` | Redis key layout |

## How to run just this part

```bash
pytest tests/test_isolation.py -v
```

With compose up, section 2 and 4 of `python scripts/demo.py` show the 403
and each tenant's own balance.

## What to look at in the output

* `GET /v1/usage` with two different keys: each response names its own
  `tenant` and lists only that tenant's `recent` requests.
* In Postgres, `select name, api_key_hash from tenants;` shows 64-character
  hashes, never the keys themselves.
