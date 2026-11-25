"""End-to-end demo against a running gateway (e.g. ``docker compose up``).

What it shows, in order:

1. One request per tenant with its own use case and its own API key:
   - Clinibud      -> administrative appointment summary  (fast-chat)
   - OmniMagistral -> structured order extraction, validated with pydantic
   - Plutus        -> financial document classification   (premium-chat)
2. Isolation: Plutus asking for a model outside its allowlist gets 403.
3. Fallback: the primary mock model is broken on purpose; the request is
   still answered, by ``backup-chat``.
4. Each tenant's own balance and usage, as only that tenant can see it.

Outputs come from the mock backend (canned text) -- the demo shows the
gateway's behaviour, not model quality. It makes no clinical decisions.

    python scripts/generate_synthetic.py
    python scripts/demo.py   # GATEWAY_URL / MOCK_URL / TENANT_KEY_* from env
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from pydantic import BaseModel

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8400")
MOCK = os.getenv("MOCK_URL", "http://localhost:8500")
DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"

# Defaults match docker-compose.yml's local development keys.
KEYS = {
    "clinibud": os.getenv("TENANT_KEY_CLINIBUD", "dev-clinibud-key"),
    "omnimagistral": os.getenv("TENANT_KEY_OMNIMAGISTRAL", "dev-omnimagistral-key"),
    "plutus": os.getenv("TENANT_KEY_PLUTUS", "dev-plutus-key"),
}

# Each use case: which model the tenant uses and the system prompt. The
# "[task:...]" marker is how the mock picks its canned answer; a real model
# would simply ignore it.
USE_CASES = {
    "clinical_summary": (
        "clinibud",
        "fast-chat",
        "[task:clinical_summary] Write a short ADMINISTRATIVE summary of the appointment "
        "(attendance, scheduling, billing). Do not give medical advice or diagnoses.",
    ),
    "order_extraction": (
        "omnimagistral",
        "fast-chat",
        "[task:order_extraction] Extract the order as JSON with keys customer_name, "
        "items[{ingredient, dose_mg, form, quantity}], delivery, urgent.",
    ),
    "doc_classification": (
        "plutus",
        "premium-chat",
        "[task:doc_classification] Classify the document as one of invoice, receipt, "
        "bank_statement, payslip. Answer JSON {label, confidence, reason}.",
    ),
}


class OrderItem(BaseModel):
    """Schema the OmniMagistral extraction must satisfy."""

    ingredient: str
    dose_mg: float
    form: str
    quantity: int


class Order(BaseModel):
    """Top-level order schema; parsing fails loudly if the output is malformed."""

    customer_name: str
    items: list[OrderItem]
    delivery: str
    urgent: bool


def first_record(use_case: str) -> dict[str, object]:
    """Read the first synthetic input for a use case."""
    with (DATA / f"{use_case}.jsonl").open(encoding="utf-8") as fh:
        return json.loads(fh.readline())


def chat(client: httpx.Client, tenant: str, model: str, system: str, text: str) -> httpx.Response:
    """Send one non-streaming chat request as ``tenant`` (its own API key)."""
    return client.post(
        f"{GATEWAY}/v1/chat/completions",
        headers={"Authorization": f"Bearer {KEYS[tenant]}"},
        json={
            "model": model,
            "max_tokens": 200,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
        },
    )


def main() -> None:
    """Run the four demo sections against a running gateway."""
    with httpx.Client(timeout=30) as client:
        print("== 1. one use case per tenant ==")
        for use_case, (tenant, model, system) in USE_CASES.items():
            record = first_record(use_case)
            r = chat(client, tenant, model, system, str(record["text"]))
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            print(f"\n[{tenant}] {use_case} via {r.headers['x-gateway-served-model']}")
            print(f"  input : {record['text']}")
            print(f"  output: {content}")
            print(f"  usage : {r.json()['usage']}")
            if use_case == "order_extraction":
                order = Order.model_validate_json(content)
                print(f"  schema: valid Order with {len(order.items)} item(s)")

        print("\n== 2. isolation: plutus requests a model outside its allowlist ==")
        r = chat(client, "plutus", "fast-chat", "hi", "hi")
        print(f"  status {r.status_code}: {r.json()['error']['message']}")

        print("\n== 3. fallback: primary 'mock-fast' is broken on purpose ==")
        client.post(f"{MOCK}/admin/faults", json={"fail_models": ["mock-fast"], "slow_models": []})
        try:
            tenant, model, system = USE_CASES["clinical_summary"]
            r = chat(client, tenant, model, system, "Synthetic appointment.")
            print(
                f"  status {r.status_code}, requested {model}, "
                f"served by {r.headers.get('x-gateway-served-model')}, "
                f"fallback={r.headers.get('x-gateway-fallback')}"
            )
        finally:
            client.post(f"{MOCK}/admin/faults", json={"fail_models": [], "slow_models": []})

        print("\n== 4. each tenant sees only its own credits and usage ==")
        for tenant, key in KEYS.items():
            h = {"Authorization": f"Bearer {key}"}
            credits = client.get(f"{GATEWAY}/v1/credits", headers=h).json()
            usage = client.get(f"{GATEWAY}/v1/usage", headers=h).json()
            print(
                f"  {tenant:14s} balance={credits['balance_millicredits']:>10} mc  "
                f"requests={usage['requests']}  tokens_today={usage['tokens_used_today']}"
            )


if __name__ == "__main__":
    main()
