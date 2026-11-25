"""FastAPI application: HTTP endpoints of the gateway.

Endpoints
---------
POST /v1/chat/completions  OpenAI-compatible chat (streaming and not)
GET  /v1/credits           caller's prepaid balance
GET  /v1/usage             caller's usage ledger and today's token count
POST /admin/credits/topup  add credits to a tenant (needs ADMIN_TOKEN)
GET  /metrics              Prometheus metrics
GET  /health               liveness

Because the chat endpoint mirrors OpenAI's, a product can switch to the
gateway by changing only ``base_url`` and ``api_key`` in its OpenAI SDK client.
"""

from __future__ import annotations

import hmac
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from redis.asyncio import BlockingConnectionPool, Redis
from sqlalchemy import select

from gateway.auth import Tenant, lookup_tenant, parse_bearer
from gateway.config import Settings, load_models, load_tenants
from gateway.credits import CreditLedger
from gateway.db import create_schema, make_engine, tenants
from gateway.limits import Limiter
from gateway.metrics import GatewayMetrics
from gateway.pipeline import ChatPipeline, GatewayError
from gateway.routing import ModelRouter
from gateway.seed import seed_tenants


class TopUp(BaseModel):
    """Admin request body for adding credits."""

    tenant: str
    amount: int = Field(gt=0, description="millicredits to add")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory; tests pass their own ``Settings``."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Build every long-lived dependency once per process.
        engine = make_engine(settings.database_url, settings.db_pool_size)
        # Bounded, blocking pool: under a burst, callers wait for a free
        # connection instead of opening one socket per concurrent request.
        redis = Redis(
            connection_pool=BlockingConnectionPool.from_url(
                settings.redis_url,
                decode_responses=True,
                max_connections=20,
                timeout=30,
                socket_timeout=settings.redis_socket_timeout,
            )
        )
        await create_schema(engine)
        if settings.seed_on_startup:
            await seed_tenants(engine, load_tenants(settings.tenants_config))
        metrics = GatewayMetrics()
        ledger = CreditLedger(engine)
        pipeline = ChatPipeline(
            settings=settings,
            router=ModelRouter(load_models(settings.models_config)),
            limiter=Limiter(redis),
            ledger=ledger,
            metrics=metrics,
        )
        app.state.engine = engine
        app.state.redis = redis
        app.state.metrics = metrics
        app.state.ledger = ledger
        app.state.pipeline = pipeline
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="healthcare-ai-gateway", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    async def authenticate(request: Request, authorization: str | None) -> Tenant:
        """Resolve the caller's tenant from the Bearer key or raise 401."""
        key = parse_bearer(authorization)
        tenant = await lookup_tenant(request.app.state.engine, key) if key else None
        if tenant is None:
            request.app.state.metrics.rejections.labels("unknown", "unauthenticated").inc()
            raise GatewayError(401, "authentication_error", "invalid or missing API key")
        return tenant

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        return Response(request.app.state.metrics.render(), media_type="text/plain; version=0.0.4")

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(request: Request, authorization: str | None = Header(default=None)) -> Response:
        started = time.perf_counter()  # TTFT and latency are measured from here
        tenant = await authenticate(request, authorization)
        try:
            body: dict[str, Any] = await request.json()
        except ValueError as exc:
            raise GatewayError(400, "invalid_request_error", "body must be JSON") from exc

        pipeline: ChatPipeline = request.app.state.pipeline
        adm = await pipeline.admit(tenant, body, started)
        assert adm.upstream is not None
        headers = {
            "x-request-id": adm.request_id,
            "x-gateway-served-model": adm.upstream.served_alias,
            "x-gateway-fallback": str(adm.upstream.served_alias != adm.requested_model).lower(),
        }
        if body.get("stream"):
            include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
            return StreamingResponse(
                pipeline.relay(adm, include_usage),
                media_type="text/event-stream",
                headers=headers,
            )
        return JSONResponse(await pipeline.complete(adm), headers=headers)

    @app.get("/v1/credits")
    async def credits(
        request: Request, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        tenant = await authenticate(request, authorization)
        balance = await request.app.state.ledger.balance(tenant.id)
        return {"tenant": tenant.name, "balance_millicredits": balance}

    @app.get("/v1/usage")
    async def usage(
        request: Request, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        tenant = await authenticate(request, authorization)
        summary = await request.app.state.ledger.usage_summary(tenant.id)
        limiter: Limiter = request.app.state.pipeline.limiter
        summary["tokens_used_today"] = await limiter.tokens_used_today(tenant.id)
        summary["daily_token_quota"] = tenant.daily_token_quota
        return {"tenant": tenant.name, **summary}

    @app.post("/admin/credits/topup")
    async def topup(
        request: Request, body: TopUp, x_admin_token: str | None = Header(default=None)
    ) -> dict[str, Any]:
        # Tenant keys can never reach this endpoint: it uses a separate secret.
        if (
            not settings.admin_token
            or not x_admin_token
            or not hmac.compare_digest(x_admin_token, settings.admin_token)
        ):
            raise GatewayError(401, "authentication_error", "admin token required")
        async with request.app.state.engine.connect() as conn:
            tenant_id = await conn.scalar(select(tenants.c.id).where(tenants.c.name == body.tenant))
        if tenant_id is None:
            raise GatewayError(404, "not_found", "unknown tenant")
        balance = await request.app.state.ledger.topup(int(tenant_id), body.amount)
        return {"tenant": body.tenant, "balance_millicredits": balance}

    return app
