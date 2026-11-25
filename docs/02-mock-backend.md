# Part 2 — The mock model backend (and what it is not)

## The problem

The production target is a **vLLM** server: it exposes the OpenAI
`/v1/chat/completions` protocol, streams tokens, and reports usage. vLLM
needs an NVIDIA GPU. The machine this demo was built and measured on has
none (Intel i7-1185G7, integrated graphics).

To still test the gateway end to end we need *something* that:

* speaks the same protocol, including SSE streaming and the final usage chunk;
* streams with a **known, configurable** latency profile;
* can be **broken on purpose** (HTTP 500, or hang) to exercise fallback.

## How it works

`mock_backend/server.py` is a ~250-line FastAPI app.

```python
await asyncio.sleep(ttft_s)  # time to first token
for i, tok in enumerate(tokens):
    if i > 0:
        await asyncio.sleep(inter_token_s)  # decode speed
    yield chunk({"content": tok})
...
if include_usage:  # OpenAI semantics
    yield final_chunk_with_usage
yield "data: [DONE]\n\n"
```

* **Latency** — `MOCK_TTFT_MS` (default 50) and `MOCK_INTER_TOKEN_MS`
  (default 10, i.e. ~100 tokens/s per stream).
* **Answers** — canned per task: the demo prompts carry a marker like
  `[task:order_extraction]`; the mock returns a fixed text or JSON for it. The
  order-extraction answer is valid JSON so the demo can validate the
  structured-output contract with pydantic.
* **Tokens** — words (split on spaces). Not a real tokenizer; it only needs to
  produce consistent usage numbers for billing.
* **Faults** — `POST /admin/faults {"fail_models": [...], "slow_models": [...], "break_models": [...]}`
  makes those model names return HTTP 500, sleep `MOCK_SLOW_MS` before
  answering, or cut the stream after `MOCK_BREAK_AFTER` tokens (default 3). Tests and `scripts/demo.py` use this.

## What this means for the numbers

Any TTFT or tokens/s measured against the mock is **the mock's configured
delay plus gateway overhead**. It says nothing about how fast a real LLM is.
The benchmark therefore runs twice — direct to the mock and through the
gateway — and the useful number is the **difference** (Part 7).

## How to run just this part

```bash
uvicorn mock_backend.server:app --port 8500
curl -N localhost:8500/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"mock-fast","stream":true,"stream_options":{"include_usage":true},
       "messages":[{"role":"user","content":"[task:doc_classification] hi"}]}'
```

You should see one `data:` line per word, then a chunk with `"usage"`, then
`data: [DONE]`.
