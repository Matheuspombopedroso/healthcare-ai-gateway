"""Fallback routing: primary errors or times out -> secondary serves."""

from __future__ import annotations

from gateway.credits import cost_of
from tests.conftest import auth, chat_body


async def test_primary_http_error_falls_back(client, set_faults):
    """Primary HTTP 500 -> served by backup-chat, billed at backup price, counted."""
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
    """Primary not streaming within stream_timeout -> served by backup-chat."""
    # The slow model sleeps 4 s before answering; stream_timeout is 2 s.
    set_faults(slow=["mock-premium"])
    r = await client.post(
        "/v1/chat/completions", json=chat_body("premium-chat"), headers=auth("K_ALPHA")
    )
    assert r.status_code == 200, r.text
    assert r.headers["x-gateway-served-model"] == "backup-chat"


async def test_all_models_failing_returns_502_and_refunds(client, set_faults):
    """Whole chain failing -> 502, credits and quota reservations returned."""
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
    """A healthy primary serves the request itself."""
    set_faults()
    r = await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_ALPHA"))
    assert r.headers["x-gateway-served-model"] == "fast-chat"


async def test_mid_stream_failure_is_not_retried_and_bills_delivered_tokens(client, set_faults):
    """Primary dies after 3 tokens: no fallback, 502, billed only for what was sent."""
    set_faults(break_=["mock-fast"])
    before = (await client.get("/v1/credits", headers=auth("K_ALPHA"))).json()
    r = await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_ALPHA"))
    assert r.status_code == 502

    event = (await client.get("/v1/usage", headers=auth("K_ALPHA"))).json()["recent"][0]
    assert event["status"] == "error"
    assert event["served_model"] == "fast-chat"  # no silent switch to backup-chat
    assert event["fallback_used"] is False
    # The mock had no chance to report usage, so the gateway billed the
    # conservative fallback: one token per delivered chunk (3) plus the
    # prompt upper bound.
    assert event["completion_tokens"] == 3
    after = (await client.get("/v1/credits", headers=auth("K_ALPHA"))).json()
    assert before["balance_millicredits"] - after["balance_millicredits"] == event["cost"] > 0
