"""Tenant isolation: one tenant can never see or spend another's resources."""

from __future__ import annotations

import asyncio

from gateway.limits import rpm_key, tokens_key
from tests.conftest import auth, chat_body


async def test_missing_or_bad_key_is_rejected(client):
    """No key or an unknown key -> 401 on every tenant endpoint."""
    assert (await client.post("/v1/chat/completions", json=chat_body())).status_code == 401
    r = await client.post(
        "/v1/chat/completions", json=chat_body(), headers={"Authorization": "Bearer nope"}
    )
    assert r.status_code == 401
    assert (await client.get("/v1/usage")).status_code == 401


async def test_model_allowlist_is_per_tenant(client):
    """Allowlists are per tenant; unknown models get the same 403."""
    # beta may only use fast-chat; premium-chat is allowed for alpha.
    r = await client.post(
        "/v1/chat/completions", json=chat_body("premium-chat"), headers=auth("K_BETA")
    )
    assert r.status_code == 403
    r = await client.post(
        "/v1/chat/completions", json=chat_body("premium-chat"), headers=auth("K_ALPHA")
    )
    assert r.status_code == 200
    # Unknown models get the same 403 (no model enumeration).
    r = await client.post(
        "/v1/chat/completions", json=chat_body("secret-model"), headers=auth("K_ALPHA")
    )
    assert r.status_code == 403


async def test_usage_and_credits_only_show_own_tenant(client):
    """A tenant only ever sees its own usage and balance."""
    for _ in range(3):
        await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_ALPHA"))
    a = (await client.get("/v1/usage", headers=auth("K_ALPHA"))).json()
    b = (await client.get("/v1/usage", headers=auth("K_BETA"))).json()
    assert a["tenant"] == "alpha" and a["requests"] == 3
    assert b["tenant"] == "beta" and b["requests"] == 0 and b["recent"] == []
    assert b["tokens_used_today"] == 0
    # Query parameters cannot select another tenant: they are simply ignored.
    b2 = (await client.get("/v1/usage?tenant=alpha", headers=auth("K_BETA"))).json()
    assert b2 == b
    cb = (await client.get("/v1/credits?tenant_id=1", headers=auth("K_BETA"))).json()
    assert cb["tenant"] == "beta" and cb["balance_millicredits"] == 1_000_000


async def test_one_tenant_exhausting_budget_does_not_affect_another(client):
    """Tenant A running out of credits mid-flood leaves tenant B untouched."""
    # "poor" floods until out of credits; "beta" running concurrently is unaffected.
    poor = [
        client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_POOR"))
        for _ in range(20)
    ]
    beta = [
        client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_BETA"))
        for _ in range(10)
    ]
    results = await asyncio.gather(*poor, *beta)
    poor_codes = [r.status_code for r in results[:20]]
    beta_codes = [r.status_code for r in results[20:]]
    assert 402 in poor_codes
    assert beta_codes == [200] * 10

    beta_usage = (await client.get("/v1/usage", headers=auth("K_BETA"))).json()
    beta_balance = (await client.get("/v1/credits", headers=auth("K_BETA"))).json()
    # beta paid exactly for its own 10 requests, nothing else.
    assert beta_usage["requests"] == 10
    assert 1_000_000 - beta_balance["balance_millicredits"] == beta_usage["cost_millicredits"]


async def test_rate_limit_of_one_tenant_does_not_throttle_another(client):
    """Tenant A being rate limited does not throttle tenant B."""
    for _ in range(5):
        await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_TIGHT_RPM"))
    r = await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_BETA"))
    assert r.status_code == 200


def test_redis_keys_are_namespaced_by_tenant():
    """Redis keys embed the tenant id, so counters never collide."""
    assert rpm_key(1, now=0) != rpm_key(2, now=0)
    assert tokens_key(1).startswith("gw:t1:") and tokens_key(2).startswith("gw:t2:")
