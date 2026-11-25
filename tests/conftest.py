"""Shared test fixtures.

Tests run against REAL Redis and PostgreSQL (the behaviours under test --
atomic Lua scripts, conditional UPDATEs, CHECK constraints -- are exactly the
things an in-memory fake could get subtly wrong). Locally they come from the
containers described in the README; in CI from GitHub Actions ``services``.

The mock model backend runs in a background thread on a free port, with tiny
latencies so the suite is fast.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace

import httpx
import pytest
import uvicorn
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.app import create_app
from gateway.config import Settings, TenantSpec
from gateway.db import metadata
from gateway.seed import seed_tenants

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://gateway:gateway@localhost:15432/gateway?ssl=disable"
)
# Database 15 so the suite never collides with a dev instance using db 0.
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:16379/15")

# Synthetic tenants used only by tests. Keys are throwaway test strings.
TEST_TENANTS = [
    TenantSpec("alpha", "K_ALPHA", ["fast-chat", "premium-chat"], 1000, 1_000_000, 1_000_000),
    TenantSpec("beta", "K_BETA", ["fast-chat"], 1000, 1_000_000, 1_000_000),
    TenantSpec("tight_rpm", "K_TIGHT_RPM", ["fast-chat"], 3, 1_000_000, 1_000_000),
    TenantSpec("tight_quota", "K_TIGHT_QUOTA", ["fast-chat"], 1000, 300, 1_000_000),
    # 60 millicredits: a request reserves ~29 and costs ~4, so a handful pass
    # and then reservations start failing with 402.
    TenantSpec("poor", "K_POOR", ["fast-chat"], 1000, 1_000_000, 60),
]
TEST_KEYS = {
    "K_ALPHA": "test-key-alpha",
    "K_BETA": "test-key-beta",
    "K_TIGHT_RPM": "test-key-tight-rpm",
    "K_TIGHT_QUOTA": "test-key-tight-quota",
    "K_POOR": "test-key-poor",
}


def auth(env_name: str) -> dict[str, str]:
    """Authorization header for a test tenant."""
    return {"Authorization": f"Bearer {TEST_KEYS[env_name]}"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def mock_backend() -> Iterator[str]:
    """Start the mock OpenAI server once per test session; yield its base URL."""
    os.environ.update(MOCK_TTFT_MS="5", MOCK_INTER_TOKEN_MS="1", MOCK_SLOW_MS="4000")
    from mock_backend.server import create_app as create_mock

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_mock(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    base = f"http://127.0.0.1:{port}"
    os.environ["MOCK_BASE_URL"] = f"{base}/v1"
    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def set_faults(mock_backend: str) -> Iterator[object]:
    """Inject failures into the mock; always cleared after the test."""

    def _set(fail: list[str] | None = None, slow: list[str] | None = None) -> None:
        httpx.post(
            f"{mock_backend}/admin/faults",
            json={"fail_models": fail or [], "slow_models": slow or []},
        ).raise_for_status()

    yield _set
    _set()


@pytest.fixture
async def client(mock_backend: str) -> AsyncIterator[httpx.AsyncClient]:
    """A gateway app on a clean database and Redis, with test tenants seeded."""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)
    await seed_tenants(engine, TEST_TENANTS, TEST_KEYS)
    await engine.dispose()
    redis = Redis.from_url(TEST_REDIS_URL)
    await redis.flushdb()
    await redis.aclose()

    settings = replace(
        Settings.from_env(),
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        admin_token="test-admin-token",
        seed_on_startup=False,
        # Generous: the suite fires bursts of concurrent requests on shared CI
        # runners and laptops; a slow box must not turn into flaky failures.
        redis_socket_timeout=30.0,
    )
    app = create_app(settings)
    # httpx's ASGITransport does not run lifespan events, so run them here.
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30
        ) as c:
            c.app = app  # type: ignore[attr-defined]  # tests inspect app.state
            yield c


def chat_body(model: str = "fast-chat", task: str = "doc_classification", **extra: object) -> dict:
    """A small chat request whose mock answer is deterministic."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": f"[task:{task}] You are a helpful assistant."},
            {"role": "user", "content": "Synthetic document number 1."},
        ],
        "max_tokens": 64,
        **extra,
    }
