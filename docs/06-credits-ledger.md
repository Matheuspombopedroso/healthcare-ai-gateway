# Part 6 — Prepaid credits and the usage ledger (PostgreSQL)

## The problem

Each product buys credits in advance; every request is paid from that
balance according to the tokens it used. Requirements:

* never let a balance go negative, even under concurrent requests;
* charge for what was actually used, at the price of the model that served;
* keep an auditable record of every charge.

## Schema (`gateway/db.py`)

| Table | Purpose |
|---|---|
| `tenants` | name, key hash, allowlist, limits |
| `credit_accounts` | one row per tenant, `balance` in integer **millicredits**, `CHECK (balance >= 0)` |
| `credit_transactions` | signed entries: `topup` (+) and `usage` (−) |
| `usage_events` | one row per request: models, tokens, cost, TTFT, latency, status |

Integers, not floats: money arithmetic must be exact.

## Reserve → settle

```python
# 1. reserve the worst case, atomically
UPDATE credit_accounts
   SET balance = balance - :worst_case
 WHERE tenant_id = :t AND balance >= :worst_case
RETURNING balance          -- no row returned => 402 insufficient_credits
```

Postgres takes a row lock for the UPDATE, so concurrent reservations on one
account are serialised and the `balance >= :worst_case` condition is always
evaluated against the latest balance.

**Worst case** (`gateway/credits.py`):

* prompt tokens ≤ characters + 8 per message (a BPE token is at least one
  character);
* completion tokens ≤ `max_tokens`;
* priced at the **most expensive model in the fallback chain**.

Because this really is an upper bound, settlement only ever gives money back:

```python
# 2. settle in ONE transaction
UPDATE credit_accounts SET balance = balance + (reserved - actual_cost)
INSERT INTO usage_events (...)
INSERT INTO credit_transactions (kind='usage', amount=-actual_cost)
```

If nothing reached the client (all models failed), `actual_cost = 0` and the
whole reservation is returned. If the backend does not report usage, the
gateway bills the prompt upper bound and one token per streamed chunk, and
never more than what was reserved.

### Trade-off

A large `max_tokens` "locks" more credit while the request runs, so a tenant
close to zero may get a 402 even though the actual answer would have been
affordable. That is the price of never overspending; clients can lower
`max_tokens` to fit.

## The proof

`tests/test_chat_and_credits.py`:

* `test_non_streaming_chat_bills_exact_token_cost` — balance drops by exactly
  `ceil((prompt·in + completion·out) / 1000)`.
* `test_concurrent_flood_never_overspends` — 30 parallel requests against a
  60-millicredit balance: some 200s, the rest 402, balance ≥ 0, and
  `initial − balance == sum(ledger costs)`.
* `test_insufficient_credits_returns_402`, `test_admin_topup_requires_admin_token`.

`tests/test_fallback.py::test_all_models_failing_returns_502_and_refunds`
checks the full refund path.

## How to run just this part

```bash
pytest tests/test_chat_and_credits.py -v
# with compose up:
psql postgresql://gateway:gateway@localhost:25432/gateway \
  -c "select tenant_id, kind, amount, request_id from credit_transactions order by id desc limit 10"
```
