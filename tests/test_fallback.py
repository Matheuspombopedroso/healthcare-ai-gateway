"""Fallback routing: primary errors or times out -> secondary serves."""

from __future__ import annotations

from gateway.credits import cost_of
from tests.conftest import auth, chat_body


async def test_primary_http_error_falls_back(client, set_faults):
    set_faults(fail=["mock-fast"])
    r = await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_ALPHA"))
    assert r.status_code == 200, r.text
    assert r.headers["x-gateway-served-model"] == "backup-chat"
    assert r.headers["x-gateway-fallback"] == "true"
    assert r.json()["model"] == "backup-chat"

    # Billed at the model that actually served the request.
    usage = r.json()["usage"]
    backup = client.app.state.pipeline.router.models["backup-chat"]
    expected = cost_of(backup, usage["prompt_tokens"], usage["completion_tokens"])
    ledger = (await client.get("/v1/usage", headers=auth("K_ALPHA"))).json()
    event = ledger["recent"][0]
    assert event["requested_model"] == "fast-chat"
    assert event["served_model"] == "backup-chat"
    assert event["fallback_used"] is True
    assert event["cost"] == expected

    metrics = (await client.get("/metrics")).text
    assert (
        'gateway_fallbacks_total{requested_model="fast-chat",served_model="backup-chat"} 1.0'
        in metrics
    )


async def test_primary_timeout_falls_back(client, set_faults):
    # The slow model sleeps 4 s before answering; stream_timeout is 2 s.
    set_faults(slow=["mock-premium"])
    r = await client.post(
        "/v1/chat/completions", json=chat_body("premium-chat"), headers=auth("K_ALPHA")
    )
    assert r.status_code == 200, r.text
    assert r.headers["x-gateway-served-model"] == "backup-chat"


async def test_all_models_failing_returns_502_and_refunds(client, set_faults):
    set_faults(fail=["mock-fast", "mock-backup"])
    before = (await client.get("/v1/credits", headers=auth("K_ALPHA"))).json()
    r = await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_ALPHA"))
    assert r.status_code == 502
    after = (await client.get("/v1/credits", headers=auth("K_ALPHA"))).json()
    assert after == before  # full refund of the reservation
    usage = (await client.get("/v1/usage", headers=auth("K_ALPHA"))).json()
    assert usage["recent"][0]["status"] == "error"
    assert usage["tokens_used_today"] == 0  # quota reservation released too


async def test_no_fallback_when_primary_healthy(client, set_faults):
    set_faults()
    r = await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_ALPHA"))
    assert r.headers["x-gateway-served-model"] == "fast-chat"
