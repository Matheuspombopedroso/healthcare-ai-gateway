# Part 9 — Synthetic data and the demo use cases

## The problem

The gateway needs realistic-looking traffic for its three tenants, but this
public repo must not contain any real patient, customer or pharmacy data.

## Synthetic inputs (`scripts/generate_synthetic.py`)

A seeded `random.Random(42)` draws from small, invented vocabularies (first
names, surnames, visit types, generic "Ingredient A/B/C", document templates).
Running the script twice produces byte-identical files:

```bash
python scripts/generate_synthetic.py          # 20 records per use case
# data/synthetic/clinical_summary.jsonl
# data/synthetic/order_extraction.jsonl
# data/synthetic/doc_classification.jsonl
```

Every record is marked "(synthetic)" in its text, and `data/` is git-ignored:
the files are always regenerated, never committed.

## The three use cases (`scripts/demo.py`)

| Tenant | Model alias | Prompt asks for | How the output is checked |
|---|---|---|---|
| clinibud | `fast-chat` | an **administrative** summary (attendance, scheduling, billing) — explicitly no medical advice | printed |
| omnimagistral | `fast-chat` | the order as JSON `{customer_name, items[...], delivery, urgent}` | parsed with the pydantic `Order` schema; a malformed answer raises |
| plutus | `premium-chat` | a label among invoice / receipt / bank_statement / payslip, as JSON | printed |

The answers come from the **mock backend's canned outputs**, so they do not
depend on the input text — the demo proves the *plumbing* (per-tenant keys,
routing, schema-validated structured output, billing), not model quality.
With a real model behind `config/models.vllm.yaml` the same script would send
the same prompts unchanged.

The demo then shows:

2. **isolation** — Plutus requesting `fast-chat` (not in its allowlist) → 403;
3. **fallback** — `mock-fast` is broken through the mock's fault endpoint and
   the Clinibud request is answered by `backup-chat`;
4. **per-tenant accounting** — each key sees only its own balance and usage.

## How to run just this part

```bash
docker compose up -d --build
python scripts/generate_synthetic.py
python scripts/demo.py
```

(On Docker Desktop for Windows with broken IPv4 forwarding, prefix
`GATEWAY_URL=http://[::1]:8400 MOCK_URL=http://[::1]:8500`.)

## What to look at in the output

Real output of one run (native gateway + mock on the Windows dev machine,
2026-11-25). The `compose` CI job runs the same script against the Docker
stack on Linux and printed identical lines (the inputs are seeded):

```text
== 1. one use case per tenant ==

[clinibud] clinical_summary via fast-chat
  input : Appointment record (synthetic). Patient Bruno Silva, routine check, arrived 7 min late, payer: health plan A. Requested a certificate for work: yes. Next visit suggested in 15 days.
  output: Administrative summary: follow-up appointment completed on schedule. Patient attended with a companion. Next visit to be booked in 30 days. Billing: covered by health plan, no co-payment pending. No clinical decision is made by this summary.
  usage : {'prompt_tokens': 47, 'completion_tokens': 36, 'total_tokens': 83}

[omnimagistral] order_extraction via fast-chat
  input : Order from Carla Lima (synthetic): 30 capsules of Ingredient D 5 mg. Delivery: courier.
  output: {"customer_name": "SYNTHETIC CUSTOMER", "items": [{"ingredient": "Ingredient A", "dose_mg": 50, "form": "capsule", "quantity": 60}, {"ingredient": "Ingredient B", "dose_mg": 10, "form": "capsule", "quantity": 30}], "delivery": "pickup", "urgent": false}
  usage : {'prompt_tokens': 29, 'completion_tokens': 26, 'total_tokens': 55}
  schema: valid Order with 2 item(s)

[plutus] doc_classification via premium-chat
  input : (synthetic) Payment receipt 9785. Amount received: R$ 3013.79 on 2026-01-03.
  output: {"label": "invoice", "confidence": 0.91, "reason": "Contains invoice number and due date."}
  usage : {'prompt_tokens': 26, 'completion_tokens': 11, 'total_tokens': 37}

== 2. isolation: plutus requests a model outside its allowlist ==
  status 403: model 'fast-chat' is not allowed for this key

== 3. fallback: primary 'mock-fast' is broken on purpose ==
  status 200, requested fast-chat, served by backup-chat, fallback=true

== 4. each tenant sees only its own credits and usage ==
  clinibud       balance=   4999975 mc  requests=2  tokens_today=140
  omnimagistral  balance=   1999991 mc  requests=1  tokens_today=55
  plutus         balance=    999970 mc  requests=1  tokens_today=37
```

Things to notice:

* each request lists `via <model>` — the alias that actually served it;
* the OmniMagistral answer passes the pydantic `Order` schema;
* the Plutus input is a *receipt* but the answer says `invoice`: the mock
  returns the same canned answer for every input. That is expected here and
  is exactly why no quality claim is made;
* section 3 says `served by backup-chat, fallback=true`;
* in section 4 each tenant's balance dropped only by its own usage, and
  clinibud shows 2 requests (its use case plus the fallback demo).
