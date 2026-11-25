"""Streaming load benchmark: TTFT, tokens/s and latency percentiles.

Closed-loop load: ``--concurrency`` workers each send streaming requests
back-to-back until ``--requests`` have completed. For every request we record:

* TTFT    -- time from sending the request to receiving the first content token
* latency -- time until the stream ends
* decode tokens/s -- completion tokens / (latency - TTFT)

and report p50/p95 plus aggregate throughput (completion tokens per second of
wall-clock time across all workers).

Run it twice -- once straight at the backend, once through the gateway -- and
the difference is the gateway's overhead (auth, Redis, Postgres, LiteLLM).

    python bench/benchmark.py --url http://localhost:8500/v1 --model mock-fast --label direct
    python bench/benchmark.py --url http://localhost:8400/v1 --model fast-chat \
        --key $TENANT_KEY_LOADTEST --label gateway

IMPORTANT: against the mock backend, TTFT and decode speed are dominated by the
mock's configured delays (MOCK_TTFT_MS, MOCK_INTER_TOKEN_MS). Those numbers say
nothing about a real model's speed; the *overhead* numbers are the meaningful
ones.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import time
from dataclasses import dataclass

import httpx

PROMPT = [
    {
        "role": "system",
        "content": "[task:clinical_summary] Write a short administrative summary.",
    },
    {"role": "user", "content": "Synthetic appointment record for load testing."},
]


@dataclass
class Sample:
    """Measurements of one streamed request."""

    ok: bool
    ttft_s: float | None = None
    latency_s: float | None = None
    completion_tokens: int = 0
    error: str | None = None


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile (no interpolation), on a sorted copy."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(1, round(p / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


async def one_request(client: httpx.AsyncClient, url: str, model: str, max_tokens: int) -> Sample:
    """Send one streaming request and measure TTFT, latency and completion tokens."""
    body = {
        "model": model,
        "messages": PROMPT,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    start = time.perf_counter()
    ttft = None
    chunks = 0
    usage_tokens = None
    try:
        async with client.stream("POST", f"{url}/chat/completions", json=body) as r:
            if r.status_code != 200:
                await r.aread()
                return Sample(ok=False, error=f"HTTP {r.status_code}")
            async for line in r.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                payload = json.loads(line[6:])
                if payload.get("usage"):
                    usage_tokens = payload["usage"]["completion_tokens"]
                for choice in payload.get("choices", []):
                    if choice.get("delta", {}).get("content"):
                        if ttft is None:
                            ttft = time.perf_counter() - start
                        chunks += 1
        latency = time.perf_counter() - start
        return Sample(
            ok=True,
            ttft_s=ttft,
            latency_s=latency,
            completion_tokens=usage_tokens if usage_tokens is not None else chunks,
        )
    except httpx.HTTPError as exc:
        return Sample(ok=False, error=type(exc).__name__)


async def run_level(args: argparse.Namespace, concurrency: int) -> dict[str, object]:
    """Warm up, then run ``--requests`` requests at one concurrency level and summarise."""
    headers = {"Authorization": f"Bearer {args.key}"} if args.key else {}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    samples: list[Sample] = []
    remaining = args.requests

    async with httpx.AsyncClient(headers=headers, timeout=60, limits=limits) as client:
        # Warm-up: open connections and let the server JIT/caches settle.
        await asyncio.gather(
            *[
                one_request(client, args.url, args.model, args.max_tokens)
                for _ in range(concurrency)
            ]
        )

        async def worker() -> None:
            """Take requests from the shared budget until none remain (closed loop)."""
            nonlocal remaining
            while remaining > 0:
                remaining -= 1
                samples.append(await one_request(client, args.url, args.model, args.max_tokens))

        wall_start = time.perf_counter()
        await asyncio.gather(*[worker() for _ in range(concurrency)])
        wall = time.perf_counter() - wall_start

    ok = [s for s in samples if s.ok and s.ttft_s is not None and s.latency_s is not None]
    ttft_ms = [s.ttft_s * 1000 for s in ok]  # type: ignore[operator]
    lat_ms = [s.latency_s * 1000 for s in ok]  # type: ignore[operator]
    decode = [
        s.completion_tokens / (s.latency_s - s.ttft_s)  # type: ignore[operator]
        for s in ok
        if s.completion_tokens > 1 and s.latency_s > s.ttft_s  # type: ignore[operator]
    ]
    total_tokens = sum(s.completion_tokens for s in ok)
    return {
        "label": args.label,
        "model": args.model,
        "concurrency": concurrency,
        "requests": len(samples),
        "errors": len(samples) - len(ok),
        "error_kinds": sorted({s.error for s in samples if s.error}),
        "wall_s": round(wall, 3),
        "req_per_s": round(len(ok) / wall, 2),
        "ttft_ms_p50": round(percentile(ttft_ms, 50), 1),
        "ttft_ms_p95": round(percentile(ttft_ms, 95), 1),
        "latency_ms_p50": round(percentile(lat_ms, 50), 1),
        "latency_ms_p95": round(percentile(lat_ms, 95), 1),
        "decode_tok_s_p50": round(statistics.median(decode), 1) if decode else None,
        "aggregate_tok_s": round(total_tokens / wall, 1),
        "avg_completion_tokens": round(total_tokens / len(ok), 1) if ok else 0,
    }


async def main() -> None:
    """Parse arguments and run every requested concurrency level."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--url", required=True, help="base URL ending in /v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--key", default=None, help="tenant API key (gateway only)")
    parser.add_argument("--concurrency", default="1,8,32", help="comma-separated levels")
    parser.add_argument("--requests", type=int, default=200, help="requests per level")
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", default=None, help="append JSON lines here")
    args = parser.parse_args()

    print(f"# python {platform.python_version()} on {platform.platform()}")
    for level in [int(x) for x in args.concurrency.split(",")]:
        result = await run_level(args, level)
        line = json.dumps(result)
        print(line, flush=True)
        if args.out:
            with open(args.out, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")


if __name__ == "__main__":
    asyncio.run(main())
