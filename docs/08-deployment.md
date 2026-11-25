# Part 8 — Deployment: Docker Compose, vLLM and CI

## Docker Compose (`docker-compose.yml`)

| Service | What | Notes |
|---|---|---|
| `gateway` | FastAPI app on :8400 | seeds tenants from `config/tenants.yaml` at startup |
| `redis` | counters | no persistence: counters expire anyway |
| `postgres` | tenants, credits, ledger | named volume `pgdata`, host port 25432 for `psql` |
| `mock-backend` | fake OpenAI server on :8500 | latency from `MOCK_TTFT_MS` / `MOCK_INTER_TOKEN_MS` |
| `bench` (profile `bench`) | load generator | runs inside the network |
| `vllm` (profile `vllm`) | real vLLM server | needs an NVIDIA GPU; **not run here** |

One Dockerfile builds a single image used by gateway, mock and bench. They
share dependencies, so one build serves all three.

Tenant keys default to obvious local placeholders (`dev-clinibud-key`, …).
Override them in `.env` (see `.env.example`); they are never committed.

```bash
docker compose up --build
python scripts/generate_synthetic.py
python scripts/demo.py
```

## Switching to vLLM

1. Run a vLLM server (on a GPU host):
   `vllm serve Qwen/Qwen2.5-1.5B-Instruct --port 8000`
   (or `docker compose --profile vllm up vllm`).
2. Point the gateway at `config/models.vllm.yaml` and set the URLs:
   ```bash
   MODELS_CONFIG=config/models.vllm.yaml
   VLLM_PRIMARY_URL=http://<gpu-host>:8000/v1
   VLLM_PREMIUM_URL=...
   VLLM_BACKUP_URL=...
   ```
3. Nothing else changes: tenants keep calling `fast-chat`.

`hosted_vllm/<model>` is LiteLLM's provider prefix for a self-hosted vLLM
server. vLLM supports `stream_options.include_usage`, which the billing path
relies on. **This configuration was written but not executed** — no GPU was
available.

## Scaling notes

The gateway process is stateless: all shared state is in Redis and Postgres,
so it scales horizontally behind a load balancer. Per-replica settings to
size: `DB_POOL_SIZE` (Postgres connections per replica) and the Redis pool
(`max_connections=20` in `gateway/app.py`).

## CI (`.github/workflows/ci.yml`)

On every push: install, `ruff check`, `ruff format --check`, `pytest`.
Redis and Postgres run as GitHub Actions **service containers**, so CI runs
the same real-infrastructure tests as a developer machine.

## Running tests locally

```bash
docker run -d --name hag-test-redis -p 16379:6379 redis:7-alpine
docker run -d --name hag-test-pg -p 15432:5432 \
  -e POSTGRES_USER=gateway -e POSTGRES_PASSWORD=gateway -e POSTGRES_DB=gateway postgres:16-alpine
pip install -e ".[dev]"
pytest -q
```

On Docker Desktop for Windows, if `127.0.0.1` port forwarding misbehaves, use
the IPv6 loopback:
`TEST_DATABASE_URL=postgresql+asyncpg://gateway:gateway@[::1]:15432/gateway?ssl=disable`
and `TEST_REDIS_URL=redis://[::1]:16379/15`.
