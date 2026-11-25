# Part 7 — Metrics and benchmarks (TTFT, tokens/s, p50/p95)

## The problem

For an LLM service, "average latency" hides what users feel. The numbers that
matter are:

* **TTFT** (time to first token) — how long before text starts appearing;
* **decode tokens/s** — how fast text streams once it started;
* **p50 / p95 latency** under concurrency — typical and tail behaviour;
* the **gateway's own overhead** — what the extra hop costs.

## Server-side metrics (`gateway/metrics.py`, `GET /metrics`)

| Metric | Type | Labels |
|---|---|---|
| `gateway_requests_total` | counter | tenant, model, status |
| `gateway_rejections_total` | counter | tenant, reason (`rate_limited`, `token_quota`, `insufficient_credits`, `model_not_allowed`, `unauthenticated`) |
| `gateway_fallbacks_total` | counter | requested_model, served_model |
| `gateway_tokens_total` | counter | tenant, model, kind (prompt/completion) |
| `gateway_credits_spent_millicredits_total` | counter | tenant |
| `gateway_ttft_seconds` | histogram | model |
| `gateway_request_latency_seconds` | histogram | model |
| `gateway_completion_tokens_per_second` | histogram | model |

TTFT is measured from the moment the request reaches the endpoint to the
first content token relayed, so it **includes** auth, Redis and Postgres
work. In Prometheus, p95 TTFT is:

```promql
histogram_quantile(0.95, sum by (le, model) (rate(gateway_ttft_seconds_bucket[5m])))
```

`tenant` is a label because there are only a handful of tenants; with
thousands it would explode cardinality and per-tenant numbers should come from
the Postgres ledger instead.

## Client-side benchmark (`bench/benchmark.py`)

Closed-loop load: N concurrent workers each send streaming requests
back-to-back. Per request it records TTFT (first content chunk), total latency
and completion tokens; per level it reports p50/p95, decode tokens/s and
aggregate tokens/s (all completion tokens / wall time).

It is run **twice** with identical settings:

1. directly against the mock backend (`--model mock-fast`);
2. through the gateway (`--model fast-chat --key <loadtest key>`).

Since the backend is the same, `gateway − direct` is the gateway's overhead.
The run happens **inside the compose network** (`docker compose run bench`),
so Docker Desktop's Windows port forwarding is not part of the measurement.

## Reading the results

Results, hardware, commands and raw output are in
[benchmarks.md](benchmarks.md). Keep in mind:

* TTFT ≈ 50 ms and ~100 tokens/s are **the mock's configured delays**, not a
  model's performance;
* the overhead column is the meaningful one for the gateway;
* everything ran on one laptop that was also running other workloads, so
  absolute numbers are indicative, not a capacity plan.

## How to run just this part

```bash
docker compose up -d --build
docker compose --profile bench run --rm bench \
  --url http://mock-backend:8500/v1 --model mock-fast --label direct
docker compose --profile bench run --rm bench \
  --url http://gateway:8400/v1 --model fast-chat --key dev-loadtest-key --label gateway
curl -s localhost:8400/metrics | grep gateway_ttft
```
