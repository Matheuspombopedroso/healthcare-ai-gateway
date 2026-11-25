"""Happy path, streaming, and prepaid-credit accounting."""

from __future__ import annotations

import asyncio
import json

from gateway.credits import cost_of
from tests.conftest import auth, chat_body


async def test_non_streaming_chat_bills_exact_token_cost(client):
    before = (await client.get("/v1/credits", headers=auth("K_ALPHA"))).json()
    r = await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_ALPHA"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert json.loads(data["choices"][0]["message"]["content"])["label"] == "invoice"
    assert r.headers["x-gateway-fallback"] == "false"

    usage = data["usage"]
    spec = client.app.state.pipeline.router.models["fast-chat"]
    expected = cost_of(spec, usage["prompt_tokens"], usage["completion_tokens"])
    after = (await client.get("/v1/credits", headers=auth("K_ALPHA"))).json()
    assert before["balance_millicredits"] - after["balance_millicredits"] == expected

    ledger = (await client.get("/v1/usage", headers=auth("K_ALPHA"))).json()
    assert ledger["requests"] == 1
    assert ledger["cost_millicredits"] == expected
    assert ledger["recent"][0]["status"] == "ok"
    assert ledger["tokens_used_today"] == usage["total_tokens"]


async def test_streaming_relays_sse_and_reports_usage(client):
    body = chat_body(stream=True, stream_options={"include_usage": True})
    chunks, usage = [], None
    async with client.stream(
        "POST", "/v1/chat/completions", json=body, headers=auth("K_ALPHA")
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        async for line in r.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            payload = json.loads(line[6:])
            usage = payload.get("usage") or usage
            chunks += [c["delta"].get("content", "") for c in payload["choices"]]
    text = "".join(chunks)
    assert json.loads(text)["label"] == "invoice"
    assert usage is not None and usage["completion_tokens"] > 1


async def test_insufficient_credits_returns_402(client):
    # "poor" has 60 millicredits; keep sending until the reservation fails.
    statuses = []
    for _ in range(20):
        r = await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_POOR"))
        statuses.append(r.status_code)
    assert 402 in statuses
    assert r.json()["error"]["type"] == "insufficient_credits"
    balance = (await client.get("/v1/credits", headers=auth("K_POOR"))).json()
    assert balance["balance_millicredits"] >= 0


async def test_concurrent_flood_never_overspends(client):
    """30 parallel requests against a 60-millicredit balance.

    Without the atomic reservation, many requests would pass the balance check
    before any debit landed. The invariant: balance never < 0 and total spend
    equals exactly what the ledger recorded.
    """
    rs = await asyncio.gather(
        *[
            client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_POOR"))
            for _ in range(30)
        ]
    )
    codes = [r.status_code for r in rs]
    assert set(codes) <= {200, 402}
    assert 402 in codes
    balance = (await client.get("/v1/credits", headers=auth("K_POOR"))).json()[
        "balance_millicredits"
    ]
    usage = (await client.get("/v1/usage", headers=auth("K_POOR"))).json()
    assert balance >= 0
    assert 60 - balance == usage["cost_millicredits"]
    assert usage["requests"] == codes.count(200)


async def test_admin_topup_requires_admin_token(client):
    r = await client.post(
        "/admin/credits/topup", json={"tenant": "poor", "amount": 1000}, headers=auth("K_POOR")
    )
    assert r.status_code == 401
    r = await client.post(
        "/admin/credits/topup",
        json={"tenant": "poor", "amount": 1000},
        headers={"x-admin-token": "test-admin-token"},
    )
    assert r.status_code == 200
    assert r.json()["balance_millicredits"] == 1060


async def test_metrics_endpoint_exposes_gateway_series(client):
    await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_ALPHA"))
    text = (await client.get("/metrics")).text
    assert 'gateway_requests_total{model="fast-chat",status="ok",tenant="alpha"} 1.0' in text
    assert "gateway_ttft_seconds_bucket" in text
    assert "gateway_completion_tokens_per_second_bucket" in text
