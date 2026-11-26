"""CPU cost per streamed request: LiteLLM Router vs a minimal httpx SSE client.

Why this exists: the load benchmark showed one gateway process saturating at
~10 requests/s. This script isolates where the CPU goes by streaming the same
mock response N times, sequentially, in this process, and measuring
``time.process_time()`` (CPU, not wall time):

* ``httpx``   -- raw HTTP + SSE line parsing, the floor for any Python client;
* ``litellm`` -- the same stream through ``litellm.Router`` as the gateway uses.

The mock runs in a separate process, so its CPU is not counted.

    python -m bench.cpu_per_request --base http://127.0.0.1:8500/v1 --n 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx

from gateway.config import ModelSpec
from gateway.routing import ModelRouter

MESSAGES = [
    {"role": "system", "content": "[task:clinical_summary] Administrative summary."},
    {"role": "user", "content": "Synthetic appointment record for load testing."},
]


async def httpx_stream(client: httpx.AsyncClient, base: str) -> int:
    """Stream one response with plain httpx; return the number of content chunks."""
    body = {"model": "mock-fast", "messages": MESSAGES, "max_tokens": 128, "stream": True}
    n = 0
    async with client.stream("POST", f"{base}/chat/completions", json=body) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: {"):
                payload = json.loads(line[6:])
                n += sum(1 for c in payload["choices"] if c["delta"].get("content"))
    return n


async def litellm_stream(router: ModelRouter) -> int:
    """Stream one response through the LiteLLM Router; return content chunks."""
    up = await router.open_stream("fast-chat", MESSAGES, 128)
    n = 0
    async for chunk in up.stream:
        n += sum(1 for c in chunk.choices or [] if getattr(c.delta, "content", None))
    return n


async def measure(label: str, fn, n: int) -> None:  # type: ignore[no-untyped-def]
    """Run ``fn`` n times after a warm-up and print CPU ms per request."""
    for _ in range(5):
        await fn()
    cpu0, wall0 = time.process_time(), time.perf_counter()
    chunks = 0
    for _ in range(n):
        chunks += await fn()
    cpu = (time.process_time() - cpu0) / n * 1000
    wall = (time.perf_counter() - wall0) / n * 1000
    print(
        json.dumps(
            {
                "client": label,
                "requests": n,
                "chunks_per_request": chunks / n,
                "cpu_ms_per_request": round(cpu, 2),
                "wall_ms_per_request": round(wall, 1),
            }
        )
    )


async def main() -> None:
    """Measure both clients against the same mock backend."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8500/v1")
    parser.add_argument("--n", type=int, default=100)
    args = parser.parse_args()

    spec = ModelSpec(
        "fast-chat",
        {"model": "openai/mock-fast", "api_base": args.base, "api_key": "x"},
        price_in_per_1k=1,
        price_out_per_1k=1,
    )
    router = ModelRouter({"fast-chat": spec})
    async with httpx.AsyncClient(timeout=30) as client:
        await measure("httpx", lambda: httpx_stream(client, args.base), args.n)
    await measure("litellm", lambda: litellm_stream(router), args.n)


if __name__ == "__main__":
    asyncio.run(main())
