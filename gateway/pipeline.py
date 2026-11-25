"""The request pipeline: admission control, upstream call, metering, billing.

Order of checks for every chat request (cheapest and least side-effecting
first, so a rejected request never touches money)::

    1. authenticate key            -> 401
    2. model in tenant allowlist   -> 403
    3. requests-per-minute limit   -> 429   (Redis)
    4. reserve daily token quota   -> 429   (Redis, atomic)
    5. reserve credits             -> 402   (Postgres, atomic)  [undo 4 on fail]
    6. open upstream stream        -> 502 if every model fails  [undo 4 and 5]
    7. relay tokens, measure TTFT / throughput
    8. settle quota and credits with the real token usage, write the ledger

Steps 7-8 run inside the response generator; settlement is shielded from
cancellation so a client disconnecting mid-stream still gets billed (and
refunded) correctly.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from gateway.auth import Tenant
from gateway.config import ModelSpec, Settings
from gateway.credits import (
    CreditLedger,
    UsageRecord,
    cost_of,
    estimate_prompt_tokens,
    worst_case_cost,
)
from gateway.limits import Limiter
from gateway.metrics import GatewayMetrics
from gateway.routing import ModelRouter, UpstreamStream


class GatewayError(Exception):
    """An error returned to the client in OpenAI's error format."""

    def __init__(self, status: int, code: str, message: str) -> None:
        """Store the HTTP status, error type code and human-readable message."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def body(self) -> dict[str, Any]:
        """The JSON body sent to the client (OpenAI error shape)."""
        return {"error": {"message": self.message, "type": self.code, "code": self.status}}


@dataclass
class Admission:
    """State carried from admission (steps 1-6) to settlement (step 8)."""

    tenant: Tenant
    request_id: str
    requested_model: str
    messages: list[dict[str, Any]]
    max_tokens: int
    prompt_bound: int
    reserved_tokens: int
    tokens_key: str
    reserved_credits: int
    started: float  # perf_counter at request arrival
    upstream: UpstreamStream | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamResult:
    """What happened while streaming, used for billing and metrics."""

    text_parts: list[str] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    content_chunks: int = 0
    ttft_s: float | None = None
    finish_reason: str | None = None
    error: str | None = None


class ChatPipeline:
    """Runs one chat request through the gateway."""

    def __init__(
        self,
        settings: Settings,
        router: ModelRouter,
        limiter: Limiter,
        ledger: CreditLedger,
        metrics: GatewayMetrics,
    ) -> None:
        """Wire the pipeline to its collaborators (all created once per process)."""
        self.settings = settings
        self.router = router
        self.limiter = limiter
        self.ledger = ledger
        self.metrics = metrics

    # ------------------------------------------------------------------ admit
    async def admit(self, tenant: Tenant, body: dict[str, Any], started: float) -> Admission:
        """Steps 2-6. Raises ``GatewayError``; undoes its own reservations."""
        model = body.get("model")
        messages = body.get("messages")
        if not isinstance(model, str) or not isinstance(messages, list) or not messages:
            raise GatewayError(400, "invalid_request_error", "'model' and 'messages' are required")

        # Unknown and not-allowed models get the same 403 on purpose: a tenant
        # should not be able to enumerate which models exist for others.
        if model not in tenant.allowed_models or model not in self.router.models:
            self.metrics.rejections.labels(tenant.name, "model_not_allowed").inc()
            raise GatewayError(
                403, "permission_error", f"model '{model}' is not allowed for this key"
            )

        # Every request is bounded so the cost reservation is bounded.
        max_tokens = int(body.get("max_tokens") or self.settings.default_max_tokens)
        if not 1 <= max_tokens <= self.settings.max_tokens_cap:
            raise GatewayError(
                400,
                "invalid_request_error",
                f"max_tokens must be 1..{self.settings.max_tokens_cap}",
            )

        if await self.limiter.hit_rate_limit(tenant.id, tenant.rpm_limit):
            self.metrics.rejections.labels(tenant.name, "rate_limited").inc()
            raise GatewayError(429, "rate_limit_error", "requests-per-minute limit exceeded")

        prompt_bound = estimate_prompt_tokens(messages)
        reserved_tokens = prompt_bound + max_tokens
        tokens_key = await self.limiter.reserve_tokens(
            tenant.id, reserved_tokens, tenant.daily_token_quota
        )
        if tokens_key is None:
            self.metrics.rejections.labels(tenant.name, "token_quota").inc()
            raise GatewayError(429, "quota_exceeded", "daily token quota exceeded")

        chain = self.router.chain(model)
        reserved_credits = worst_case_cost(chain, prompt_bound, max_tokens)
        if not await self.ledger.reserve(tenant.id, reserved_credits):
            await self.limiter.settle_tokens(tokens_key, reserved_tokens, 0)
            self.metrics.rejections.labels(tenant.name, "insufficient_credits").inc()
            raise GatewayError(402, "insufficient_credits", "not enough prepaid credits")

        # Pass through a small, safe subset of sampling parameters.
        params = {k: body[k] for k in ("temperature", "top_p", "stop") if k in body}
        adm = Admission(
            tenant=tenant,
            request_id=f"req-{uuid.uuid4().hex[:16]}",
            requested_model=model,
            messages=messages,
            max_tokens=max_tokens,
            prompt_bound=prompt_bound,
            reserved_tokens=reserved_tokens,
            tokens_key=tokens_key,
            reserved_credits=reserved_credits,
            started=started,
            params=params,
        )

        try:
            adm.upstream = await self.router.open_stream(model, messages, max_tokens, **params)
        except Exception as exc:  # every model in the chain failed
            await self._finish(adm, StreamResult(error=type(exc).__name__))
            raise GatewayError(
                502, "upstream_error", "all models in the fallback chain failed"
            ) from exc
        return adm

    # ----------------------------------------------------------------- stream
    async def relay(self, adm: Admission, include_usage: bool) -> AsyncIterator[str]:
        """Steps 7-8: yield OpenAI-format SSE lines and settle at the end."""
        assert adm.upstream is not None
        result = StreamResult()
        served = adm.upstream.served_alias
        created = int(time.time())

        def sse(choices: list[dict[str, Any]], usage: dict[str, int] | None = None) -> str:
            payload: dict[str, Any] = {
                "id": adm.request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": served,
                "choices": choices,
            }
            if usage is not None:
                payload["usage"] = usage
            return f"data: {json.dumps(payload)}\n\n"

        try:
            async for chunk in adm.upstream.stream:
                usage = getattr(chunk, "usage", None)
                if usage:
                    result.prompt_tokens = int(usage.prompt_tokens)
                    result.completion_tokens = int(usage.completion_tokens)
                for choice in chunk.choices or []:
                    content = getattr(choice.delta, "content", None)
                    if content:
                        if result.ttft_s is None:
                            result.ttft_s = time.perf_counter() - adm.started
                        result.content_chunks += 1
                        result.text_parts.append(content)
                        yield sse(
                            [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
                        )
                    if choice.finish_reason:
                        result.finish_reason = choice.finish_reason
            yield sse([{"index": 0, "delta": {}, "finish_reason": result.finish_reason or "stop"}])
            if include_usage:
                p, c = self._final_tokens(adm, result)
                yield sse(
                    [], usage={"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
                )
            yield "data: [DONE]\n\n"
        except Exception as exc:  # mid-stream failure: no fallback, see routing.py
            result.error = type(exc).__name__
            yield f"data: {json.dumps({'error': {'message': 'upstream failed mid-stream'}})}\n\n"
        finally:
            # Shield: if the client disconnects, the generator is cancelled,
            # but the money bookkeeping must still complete.
            await asyncio.shield(self._finish(adm, result))

    async def complete(self, adm: Admission) -> dict[str, Any]:
        """Non-streaming response: consume the relay and assemble one JSON body.

        The gateway always streams from the backend, even for non-streaming
        clients, so TTFT is measured the same way for every request.
        """
        content: list[str] = []
        finish = "stop"
        usage: dict[str, int] = {}
        # aclosing() guarantees the relay generator is closed -- and therefore
        # settles -- right here, even when we raise out of the loop below,
        # instead of whenever the garbage collector finalizes it.
        async with aclosing(self.relay(adm, include_usage=True)) as lines:
            async for line in lines:
                if not line.startswith("data: {"):
                    continue
                payload = json.loads(line[6:])
                if "error" in payload:
                    raise GatewayError(502, "upstream_error", payload["error"]["message"])
                usage = payload.get("usage") or usage
                for ch in payload.get("choices", []):
                    content.append(ch["delta"].get("content") or "")
                    finish = ch.get("finish_reason") or finish
        assert adm.upstream is not None
        return {
            "id": adm.request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": adm.upstream.served_alias,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(content)},
                    "finish_reason": finish,
                }
            ],
            "usage": usage,
        }

    # ----------------------------------------------------------------- settle
    def _final_tokens(self, adm: Admission, result: StreamResult) -> tuple[int, int]:
        """Token counts to bill.

        Prefer the backend's usage report. If it is missing (some servers do
        not support ``include_usage``), fall back to conservative numbers: the
        prompt upper bound and one token per streamed chunk.
        """
        prompt = result.prompt_tokens if result.prompt_tokens is not None else adm.prompt_bound
        completion = (
            result.completion_tokens
            if result.completion_tokens is not None
            else result.content_chunks
        )
        # Never bill above what was reserved (keeps the "balance >= 0" invariant
        # even if a backend over-reports usage).
        return min(prompt, adm.prompt_bound), min(completion, adm.max_tokens)

    async def _finish(self, adm: Admission, result: StreamResult) -> None:
        """Step 8: settle quota + credits, write the ledger, record metrics."""
        tenant = adm.tenant
        latency = time.perf_counter() - adm.started
        delivered = adm.upstream is not None and result.content_chunks > 0
        served_alias = adm.upstream.served_alias if adm.upstream else None

        if delivered and served_alias is not None:
            prompt, completion = self._final_tokens(adm, result)
            spec: ModelSpec = self.router.models[served_alias]
            cost = cost_of(spec, prompt, completion)
        else:
            # Nothing reached the client: do not charge anything.
            prompt, completion, cost = 0, 0, 0

        status = "ok" if result.error is None and delivered else "error"
        fallback_used = served_alias is not None and served_alias != adm.requested_model

        await self.limiter.settle_tokens(adm.tokens_key, adm.reserved_tokens, prompt + completion)
        await self.ledger.settle(
            tenant.id,
            adm.reserved_credits,
            UsageRecord(
                request_id=adm.request_id,
                requested_model=adm.requested_model,
                served_model=served_alias,
                fallback_used=fallback_used,
                status=status,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cost=cost,
                ttft_ms=None if result.ttft_s is None else result.ttft_s * 1000,
                latency_ms=latency * 1000,
            ),
        )

        m = self.metrics
        m.requests.labels(tenant.name, adm.requested_model, status).inc()
        if fallback_used:
            m.fallbacks.labels(adm.requested_model, served_alias).inc()
        if delivered and served_alias is not None:
            m.tokens.labels(tenant.name, served_alias, "prompt").inc(prompt)
            m.tokens.labels(tenant.name, served_alias, "completion").inc(completion)
            m.credits_spent.labels(tenant.name).inc(cost)
            m.latency.labels(served_alias).observe(latency)
            if result.ttft_s is not None:
                m.ttft.labels(served_alias).observe(result.ttft_s)
                decode_time = latency - result.ttft_s
                if completion > 1 and decode_time > 0:
                    m.tokens_per_second.labels(served_alias).observe(completion / decode_time)
