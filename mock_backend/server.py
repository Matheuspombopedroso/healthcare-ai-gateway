"""Mock OpenAI-compatible model server.

Why this exists
---------------
The target backend for this gateway is a vLLM server (which speaks the OpenAI
``/v1/chat/completions`` protocol). vLLM needs a GPU, and the development
machine for this demo has none. To still exercise the *gateway* end to end --
routing, streaming, fallback, token accounting, metrics -- we run this small
server that speaks the same protocol and streams tokens with a configurable,
deterministic latency profile.

What it does NOT do: it does not run a language model. Its answers are canned
templates chosen by a task marker in the system prompt. Any latency number
measured against it describes the gateway plus this mock's configured delays,
never the speed of a real LLM.

Knobs (environment variables)
-----------------------------
MOCK_TTFT_MS          delay before the first token (default 50)
MOCK_INTER_TOKEN_MS   delay between tokens (default 10)
MOCK_FAIL_MODELS      comma-separated model names that always return HTTP 500
MOCK_SLOW_MODELS      comma-separated model names that sleep MOCK_SLOW_MS
                      before answering (used to trigger gateway timeouts)
MOCK_SLOW_MS          how long "slow" models sleep (default 5000)
MOCK_BREAK_MODELS     comma-separated model names whose stream is cut after
                      MOCK_BREAK_AFTER tokens (default 3), simulating a
                      backend crash in the middle of a response

The fail/slow lists can also be changed at runtime through ``POST /admin/faults``
so tests and demos can break the primary model without restarting anything.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Canned outputs per demo task. The gateway's demo prompts put a marker such as
# "[task:clinical_summary]" in the system message; the mock looks for it.
# ---------------------------------------------------------------------------
CANNED_OUTPUTS: dict[str, str] = {
    # Clinibud: administrative (non-clinical) summary of an appointment.
    "clinical_summary": (
        "Administrative summary: follow-up appointment completed on schedule. "
        "Patient attended with a companion. Next visit to be booked in 30 days. "
        "Billing: covered by health plan, no co-payment pending. "
        "No clinical decision is made by this summary."
    ),
    # OmniMagistral: structured extraction of a compounding-pharmacy order.
    # Valid JSON so the demo can validate it against a pydantic schema.
    "order_extraction": json.dumps(
        {
            "customer_name": "SYNTHETIC CUSTOMER",
            "items": [
                {"ingredient": "Ingredient A", "dose_mg": 50, "form": "capsule", "quantity": 60},
                {"ingredient": "Ingredient B", "dose_mg": 10, "form": "capsule", "quantity": 30},
            ],
            "delivery": "pickup",
            "urgent": False,
        }
    ),
    # Plutus: classification of a financial document.
    "doc_classification": json.dumps(
        {"label": "invoice", "confidence": 0.91, "reason": "Contains invoice number and due date."}
    ),
}
DEFAULT_OUTPUT = (
    "This is a synthetic response from the mock backend. It streams tokens so the "
    "gateway can measure time to first token and throughput."
)


def _env_set(name: str) -> set[str]:
    """Parse a comma-separated environment variable into a set of names."""
    return {x.strip() for x in os.getenv(name, "").split(",") if x.strip()}


class Faults(BaseModel):
    """Runtime fault-injection state (which models fail or hang)."""

    fail_models: set[str] = Field(default_factory=set)
    slow_models: set[str] = Field(default_factory=set)
    break_models: set[str] = Field(default_factory=set)


class ChatMessage(BaseModel):
    """One OpenAI-style chat message."""

    role: str
    content: str | None = ""


class ChatRequest(BaseModel):
    """Subset of the OpenAI chat-completions request the mock understands."""

    model: str
    messages: list[ChatMessage]
    stream: bool = False
    max_tokens: int | None = None
    stream_options: dict[str, Any] | None = None


def count_tokens(text: str) -> int:
    """Approximate token count: whitespace-separated words.

    A real tokenizer is not needed here -- the mock only has to report *some*
    consistent usage numbers so the gateway's billing path is exercised.
    """
    return max(1, len(text.split()))


def pick_output(messages: list[ChatMessage]) -> str:
    """Choose a canned answer based on the ``[task:...]`` marker, if any."""
    joined = " ".join(m.content or "" for m in messages)
    for task, text in CANNED_OUTPUTS.items():
        if f"[task:{task}]" in joined:
            return text
    return DEFAULT_OUTPUT


def tokenize_output(text: str, max_tokens: int | None) -> list[str]:
    """Split the answer into word-level "tokens", honoring ``max_tokens``.

    Each token keeps its trailing space so concatenating chunks reproduces the
    original text exactly (important for the JSON outputs).
    """
    words = text.split(" ")
    tokens = [w + (" " if i < len(words) - 1 else "") for i, w in enumerate(words)]
    if max_tokens is not None:
        tokens = tokens[:max_tokens]
    return tokens


def create_app() -> FastAPI:
    """Build the FastAPI app. A factory so tests can start isolated instances."""
    app = FastAPI(title="mock-openai-backend")
    faults = Faults(
        fail_models=_env_set("MOCK_FAIL_MODELS"),
        slow_models=_env_set("MOCK_SLOW_MODELS"),
        break_models=_env_set("MOCK_BREAK_MODELS"),
    )
    ttft_s = float(os.getenv("MOCK_TTFT_MS", "50")) / 1000
    inter_token_s = float(os.getenv("MOCK_INTER_TOKEN_MS", "10")) / 1000
    slow_s = float(os.getenv("MOCK_SLOW_MS", "5000")) / 1000
    break_after = int(os.getenv("MOCK_BREAK_AFTER", "3"))

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        """Minimal ``/v1/models`` so OpenAI clients that probe it do not fail."""
        # Some clients probe this endpoint; any model name is accepted.
        return {"object": "list", "data": []}

    @app.post("/admin/faults")
    async def set_faults(new: Faults) -> Faults:
        """Replace the fault-injection state (used by tests and the demo)."""
        faults.fail_models = new.fail_models
        faults.slow_models = new.slow_models
        faults.break_models = new.break_models
        return faults

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(req: ChatRequest) -> JSONResponse | StreamingResponse:
        """Serve one chat completion, streaming or not, after fault injection."""
        # Fault injection happens before any byte is sent, which is exactly the
        # failure mode the gateway's fallback is designed to handle.
        if req.model in faults.fail_models:
            raise HTTPException(status_code=500, detail=f"injected failure for {req.model}")
        if req.model in faults.slow_models:
            await asyncio.sleep(slow_s)

        prompt_tokens = sum(count_tokens(m.content or "") for m in req.messages)
        tokens = tokenize_output(pick_output(req.messages), req.max_tokens)
        completion_tokens = len(tokens)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        if not req.stream:
            await asyncio.sleep(ttft_s + inter_token_s * max(0, completion_tokens - 1))
            return JSONResponse(
                {
                    "id": resp_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": req.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "".join(tokens)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage,
                }
            )

        include_usage = bool((req.stream_options or {}).get("include_usage"))

        async def event_stream():  # type: ignore[no-untyped-def]
            """Yield SSE lines: delayed first token, paced tokens, usage, ``[DONE]``."""

            def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
                """Format one OpenAI ``chat.completion.chunk`` as an SSE ``data:`` line."""
                body = {
                    "id": resp_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": req.model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                return f"data: {json.dumps(body)}\n\n"

            await asyncio.sleep(ttft_s)
            breaks = req.model in faults.break_models
            for i, tok in enumerate(tokens):
                if breaks and i == break_after:
                    # Abort mid-response: the HTTP stream ends without a
                    # finish chunk, usage or [DONE], like a crashed server.
                    raise RuntimeError(f"injected mid-stream failure for {req.model}")
                if i > 0:
                    await asyncio.sleep(inter_token_s)
                delta = {"role": "assistant", "content": tok} if i == 0 else {"content": tok}
                yield chunk(delta)
            yield chunk({}, finish="stop")
            # OpenAI semantics: with include_usage, a final chunk has empty
            # choices and the usage object. The gateway bills from this.
            if include_usage:
                final = {
                    "id": resp_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": req.model,
                    "choices": [],
                    "usage": usage,
                }
                yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


app = create_app()
