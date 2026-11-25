# Part 3 — Routing and fallback with LiteLLM

## The problem

Tenants should ask for a *capability* ("fast-chat"), not a server address.
Behind that name the platform team must be free to swap the backend (mock →
vLLM → hosted API) and to survive the primary model being down.

## How it works

`config/models.yaml` maps each **public alias** to a LiteLLM deployment, a
price and a fallback list:

```yaml
- alias: fast-chat
  litellm_params:
    model: openai/mock-fast      # any OpenAI-compatible server
    api_base: ${MOCK_BASE_URL}
    stream_timeout: 2            # stream must start within 2 s
  price_in_per_1k: 100           # millicredits
  price_out_per_1k: 200
  fallbacks: [backup-chat]
```

`gateway/routing.py` turns that into a `litellm.Router`:

```python
Router(
    model_list=[{"model_name": alias, "litellm_params": ...}, ...],
    fallbacks=[{"fast-chat": ["backup-chat"]}, ...],
    num_retries=0,          # failing primary goes straight to the fallback
    disable_cooldowns=True, # one error must not bench a deployment for minutes
)
```

After the call, the gateway reads which deployment actually answered
(`stream._hidden_params["litellm_model_name"]`) and maps it back to an alias.
That is used to:

* **bill at the price of the model that served** the request;
* set response headers `x-gateway-served-model` and `x-gateway-fallback`;
* increment `gateway_fallbacks_total{requested_model, served_model}`.

### Library, not the LiteLLM proxy

LiteLLM also ships a proxy server with its own virtual keys and budgets. This
project uses the **library** on purpose: the multi-tenant layer (keys, quotas,
credits, isolation) is what is being demonstrated, so it is implemented here,
explicitly, and LiteLLM does what it is best at — one interface over many
backends, plus timeouts and fallbacks.

### What triggers a fallback (verified in `tests/test_fallback.py`)

| Situation | Result |
|---|---|
| primary returns HTTP 500 before streaming | served by `backup-chat` |
| primary does not start streaming within `stream_timeout` | served by `backup-chat` |
| primary and backup both fail | HTTP 502, reservation fully refunded |
| primary fails **after** tokens were sent | no fallback; error event; billed for what was delivered |

The last row is a deliberate trade-off: once the client has seen partial
output, silently continuing with a different model would splice two answers.

## How to run just this part

```bash
pytest tests/test_fallback.py -v
```

Or with the compose stack up, `python scripts/demo.py` — section 3 breaks
`mock-fast` and shows the request still answered by `backup-chat`.
