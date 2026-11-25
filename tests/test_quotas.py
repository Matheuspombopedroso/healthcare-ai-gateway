"""Rate limits and daily token quotas (Redis)."""

from __future__ import annotations

import asyncio

from tests.conftest import auth, chat_body


async def test_rate_limit_returns_429_after_rpm(client):
    codes = [
        (
            await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_TIGHT_RPM"))
        ).status_code
        for _ in range(7)
    ]
    # rpm_limit is 3. Fixed window: if the minute rolls over mid-test the
    # count restarts once, but 7 requests still guarantee at least one 429.
    assert codes[:3] == [200, 200, 200]
    assert 429 in codes[3:]


async def test_rejected_requests_do_not_touch_credits(client):
    before = (await client.get("/v1/credits", headers=auth("K_TIGHT_RPM"))).json()
    for _ in range(3):
        await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_TIGHT_RPM"))
    after_ok = (await client.get("/v1/credits", headers=auth("K_TIGHT_RPM"))).json()
    for _ in range(3):
        r = await client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_TIGHT_RPM"))
        assert r.status_code == 429
    after_rejected = (await client.get("/v1/credits", headers=auth("K_TIGHT_RPM"))).json()
    assert after_ok["balance_millicredits"] < before["balance_millicredits"]
    assert after_rejected == after_ok  # 429s cost nothing


async def test_token_quota_is_enforced_and_atomic(client):
    """Quota is 300 tokens/day; each request reserves prompt bound + 64."""
    rs = await asyncio.gather(
        *[
            client.post("/v1/chat/completions", json=chat_body(), headers=auth("K_TIGHT_QUOTA"))
            for _ in range(20)
        ]
    )
    codes = [r.status_code for r in rs]
    assert 429 in codes
    assert all(r.json()["error"]["type"] == "quota_exceeded" for r in rs if r.status_code == 429)
    usage = (await client.get("/v1/usage", headers=auth("K_TIGHT_QUOTA"))).json()
    # Reservations were settled down to real usage, which stays under quota.
    assert 0 < usage["tokens_used_today"] <= 300
    assert usage["tokens_used_today"] == usage["prompt_tokens"] + usage["completion_tokens"]


async def test_max_tokens_is_bounded(client):
    r = await client.post(
        "/v1/chat/completions", json=chat_body(max_tokens=100_000), headers=auth("K_ALPHA")
    )
    assert r.status_code == 400
