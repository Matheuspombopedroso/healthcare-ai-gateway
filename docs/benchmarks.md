# Benchmarks (raw)

Everything on this page was measured on one laptop on 2026-11-25. Read
[Part 7](07-metrics-and-benchmarks.md) for what the metrics mean.

## What was measured — and what was not

* **Backend: the mock OpenAI-compatible server** (`mock_backend/server.py`)
  with `MOCK_TTFT_MS=50` and `MOCK_INTER_TOKEN_MS=10`. Every answer is the same
  canned 36-token text. **No language model was run.** TTFT and tokens/s in the
  "direct" rows describe the mock's configured delays on this OS, not any LLM.
* The meaningful numbers are **gateway minus direct**: the cost of auth,
  Redis (rate limit + atomic quota), Postgres (credit reservation +
  settlement + ledger rows), LiteLLM routing and the SSE relay.
* vLLM: **not measured** (no GPU).

## Hardware and setup

| | |
|---|---|
| CPU | 11th Gen Intel Core i7-1185G7 @ 3.00 GHz, 4 cores / 8 threads |
| RAM | 39.7 GiB (42,638,303,232 bytes) |
| OS | Windows 11 Pro 10.0.26200 |
| Python | 3.13.1 (Windows default asyncio event loop; no uvloop) |
| Redis | 5.0.14.1, native Windows build, port 16380, no persistence |
| PostgreSQL | 14, native Windows binaries, local cluster on port 15433 |
| Gateway | `uvicorn gateway.main:app`, 1 process (then 4 workers), `--no-access-log` |
| Load client | `bench/benchmark.py` on the same machine (shares the CPU) |

Why native processes and not the compose stack: during this session Docker
Desktop on this machine repeatedly returned HTTP 500/502 from its API and its
Windows port forwarder dropped connections under load (see Limitations in the
README). Running everything natively removed that noise source. The laptop was
also running other workloads at the time (browser, IDE, other Docker
containers, another project's test run), so treat absolute numbers as
indicative.

Two platform effects visible in the "direct" rows:

* **Windows timer resolution.** `asyncio.sleep(0.010)` measured 16.57 ms
  on this machine (`asyncio.sleep(0.050)` → 56.95 ms). That is why the mock
  configured for 100 tokens/s actually streams ~66 tokens/s and its 50 ms TTFT
  shows as ~61 ms.
* Each request streams 36 completion tokens (`avg_completion_tokens`).

## Commands

```bash
# terminal 1: mock backend
MOCK_TTFT_MS=50 MOCK_INTER_TOKEN_MS=10 \
  uvicorn mock_backend.server:app --host 127.0.0.1 --port 8500 --no-access-log

# terminal 2: gateway (single process; later re-run with --workers 4 and DB_POOL_SIZE=10)
DATABASE_URL="postgresql+asyncpg://gateway:gateway@127.0.0.1:15433/gateway_bench?ssl=disable" \
REDIS_URL="redis://127.0.0.1:16380/0?protocol=2" MOCK_BASE_URL="http://127.0.0.1:8500/v1" \
TENANT_KEY_LOADTEST=dev-loadtest-key DB_POOL_SIZE=20 \
  uvicorn gateway.main:app --host 127.0.0.1 --port 8400 --no-access-log

# terminal 3: load
python bench/benchmark.py --url http://127.0.0.1:8500/v1 --model mock-fast \
  --concurrency 1,8,32 --requests 200 --label direct
python bench/benchmark.py --url http://127.0.0.1:8400/v1 --model fast-chat \
  --key dev-loadtest-key --concurrency 1,8,32 --requests 200 --label gateway
python -m bench.cpu_per_request --base http://127.0.0.1:8500/v1 --n 100
```

(The other `TENANT_KEY_*` variables were also set, to the compose defaults; the
`loadtest` tenant has limits high enough that no request was rate limited.)

## Summary

Latency in ms, 200 streamed requests per row (after a warm-up of one request
per worker), closed loop, 0 errors in every row.

| Setup | Conc. | req/s | TTFT p50 | TTFT p95 | Latency p50 | Latency p95 | decode tok/s p50 | aggregate tok/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| direct to mock | 1 | 1.64 | 61.6 | 64.1 | 607.1 | 635.2 | 66.0 | 58.9 |
| gateway, 1 process | 1 | 1.47 | 77.8 | 87.9 | 678.9 | 731.4 | 59.9 | 52.9 |
| direct to mock | 8 | 13.13 | 60.3 | 64.4 | 606.5 | 624.7 | 65.9 | 472.8 |
| gateway, 1 process | 8 | 9.12 | 145.4 | 208.9 | 866.4 | 999.8 | 50.2 | 328.4 |
| gateway, 4 workers | 8 | 10.45 | 97.6 | 130.9 | 734.3 | 871.6 | 56.8 | 376.3 |
| direct to mock | 32 | 45.94 | 62.6 | 193.8 | 617.0 | 751.2 | 65.4 | 1653.8 |
| gateway, 1 process | 32 | 10.72 | 1048.7 | 2740.1 | 2634.6 | 4841.8 | 24.3 | 385.9 |
| gateway, 4 workers | 32 | 28.94 | 187.2 | 553.1 | 991.1 | 1521.5 | 46.7 | 1042.0 |

Gateway overhead at concurrency 1 (gateway − direct): **TTFT +16.2 ms p50 /
+23.8 ms p95; total latency +71.8 ms p50 / +96.2 ms p95.**

CPU cost per streamed request (36 chunks, sequential, measured with
`time.process_time()` in the client process):

| Client | CPU ms / request |
|---|---:|
| plain `httpx` + SSE parsing | 20.78 |
| `litellm.Router` streaming (as used by the gateway) | 78.91 |

### Interpretation

* At low load the gateway adds ~16 ms before the first token and ~72 ms over
  a 36-token stream (p50).
* **One gateway process is CPU-bound at ~10 requests/s** on this laptop.
  The microbenchmark explains most of it: consuming a stream through LiteLLM
  costs ~79 ms of CPU per request versus ~21 ms for a bare httpx client (about
  1.6 ms of extra CPU per streamed chunk). Under 32 concurrent streams the
  event loop is saturated, so TTFT and decode rate degrade sharply.
* **Scaling out works because the gateway is stateless**: 4 uvicorn workers
  sharing Redis and Postgres served 2.7× the throughput at concurrency 32
  (28.94 vs 10.72 req/s) and cut p95 latency from 4.8 s to 1.5 s. The laptop
  has 4 physical cores and was also running the mock and the load client.
* With a real model the backend's own TTFT (hundreds of ms) and decode speed
  would dominate; the gateway's per-chunk CPU cost would then matter mainly for
  how many concurrent streams one process can relay.

## Raw output

```text
2026-11-25T19:30:55-03:00
$ python bench/benchmark.py --url http://127.0.0.1:8500/v1 --model mock-fast --concurrency 1,8,32 --requests 200 --label direct
# python 3.13.1 on Windows-11-10.0.26200-SP0
{"label": "direct", "model": "mock-fast", "concurrency": 1, "requests": 200, "errors": 0, "error_kinds": [], "wall_s": 122.201, "req_per_s": 1.64, "ttft_ms_p50": 61.6, "ttft_ms_p95": 64.1, "latency_ms_p50": 607.1, "latency_ms_p95": 635.2, "decode_tok_s_p50": 66.0, "aggregate_tok_s": 58.9, "avg_completion_tokens": 36.0}
{"label": "direct", "model": "mock-fast", "concurrency": 8, "requests": 200, "errors": 0, "error_kinds": [], "wall_s": 15.227, "req_per_s": 13.13, "ttft_ms_p50": 60.3, "ttft_ms_p95": 64.4, "latency_ms_p50": 606.5, "latency_ms_p95": 624.7, "decode_tok_s_p50": 65.9, "aggregate_tok_s": 472.8, "avg_completion_tokens": 36.0}
{"label": "direct", "model": "mock-fast", "concurrency": 32, "requests": 200, "errors": 0, "error_kinds": [], "wall_s": 4.354, "req_per_s": 45.94, "ttft_ms_p50": 62.6, "ttft_ms_p95": 193.8, "latency_ms_p50": 617.0, "latency_ms_p95": 751.2, "decode_tok_s_p50": 65.4, "aggregate_tok_s": 1653.8, "avg_completion_tokens": 36.0}
$ python bench/benchmark.py --url http://127.0.0.1:8400/v1 --model fast-chat --key dev-loadtest-key --concurrency 1,8,32 --requests 200 --label gateway
# python 3.13.1 on Windows-11-10.0.26200-SP0
{"label": "gateway", "model": "fast-chat", "concurrency": 1, "requests": 200, "errors": 0, "error_kinds": [], "wall_s": 136.14, "req_per_s": 1.47, "ttft_ms_p50": 77.8, "ttft_ms_p95": 87.9, "latency_ms_p50": 678.9, "latency_ms_p95": 731.4, "decode_tok_s_p50": 59.9, "aggregate_tok_s": 52.9, "avg_completion_tokens": 36.0}
{"label": "gateway", "model": "fast-chat", "concurrency": 8, "requests": 200, "errors": 0, "error_kinds": [], "wall_s": 21.922, "req_per_s": 9.12, "ttft_ms_p50": 145.4, "ttft_ms_p95": 208.9, "latency_ms_p50": 866.4, "latency_ms_p95": 999.8, "decode_tok_s_p50": 50.2, "aggregate_tok_s": 328.4, "avg_completion_tokens": 36.0}
{"label": "gateway", "model": "fast-chat", "concurrency": 32, "requests": 200, "errors": 0, "error_kinds": [], "wall_s": 18.66, "req_per_s": 10.72, "ttft_ms_p50": 1048.7, "ttft_ms_p95": 2740.1, "latency_ms_p50": 2634.6, "latency_ms_p95": 4841.8, "decode_tok_s_p50": 24.3, "aggregate_tok_s": 385.9, "avg_completion_tokens": 36.0}
# gateway restarted with: uvicorn gateway.main:app --workers 4 (DB_POOL_SIZE=10 per worker)
$ python bench/benchmark.py --url http://127.0.0.1:8400/v1 --model fast-chat --key dev-loadtest-key --concurrency 8,32 --requests 200 --label gateway-4workers
# python 3.13.1 on Windows-11-10.0.26200-SP0
{"label": "gateway-4workers", "model": "fast-chat", "concurrency": 8, "requests": 200, "errors": 0, "error_kinds": [], "wall_s": 19.133, "req_per_s": 10.45, "ttft_ms_p50": 97.6, "ttft_ms_p95": 130.9, "latency_ms_p50": 734.3, "latency_ms_p95": 871.6, "decode_tok_s_p50": 56.8, "aggregate_tok_s": 376.3, "avg_completion_tokens": 36.0}
{"label": "gateway-4workers", "model": "fast-chat", "concurrency": 32, "requests": 200, "errors": 0, "error_kinds": [], "wall_s": 6.91, "req_per_s": 28.94, "ttft_ms_p50": 187.2, "ttft_ms_p95": 553.1, "latency_ms_p50": 991.1, "latency_ms_p95": 1521.5, "decode_tok_s_p50": 46.7, "aggregate_tok_s": 1042.0, "avg_completion_tokens": 36.0}
$ python -m bench.cpu_per_request --base http://127.0.0.1:8500/v1 --n 100
{"client": "httpx", "requests": 100, "chunks_per_request": 36.0, "cpu_ms_per_request": 20.78, "wall_ms_per_request": 754.8}
{"client": "litellm", "requests": 100, "chunks_per_request": 36.0, "cpu_ms_per_request": 78.91, "wall_ms_per_request": 690.9}
```

Timer check (same machine, same session):

```text
$ python -c "...50 x asyncio.sleep(d)..."
10.0 ms requested -> 16.57 ms actual
50.0 ms requested -> 56.95 ms actual
```
