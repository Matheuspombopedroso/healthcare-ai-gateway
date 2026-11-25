"""Model routing and fallback, delegated to the LiteLLM ``Router``.

Why LiteLLM as a *library* (not the LiteLLM proxy server): the proxy ships its
own virtual keys, budgets and database schema. This project's point is to show
the multi-tenant layer (keys, quotas, credits, isolation), so we own that layer
and use LiteLLM for what it is best at -- one OpenAI-style interface over many
backends (vLLM, OpenAI-compatible servers, hosted APIs) plus retries, timeouts
and fallbacks between deployments.

Fallback semantics (verified by ``tests/test_fallback.py``):

* the primary returns an error (e.g. HTTP 500) before streaming -> the next
  model in its ``fallbacks`` list is called;
* the primary does not start streaming within ``stream_timeout`` -> same.
* an error *after* tokens were already sent to the client is NOT retried on
  another model: the client has already seen partial output, and silently
  splicing two models' answers would be worse than failing. The request is
  billed for what was delivered.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# LiteLLM downloads a model price map from the internet at import time unless
# told to use its bundled copy. We price requests ourselves, and a network
# fetch at startup slows boot and breaks offline runs.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from litellm import Router  # noqa: E402  (must come after the env var above)

from gateway.config import ModelSpec  # noqa: E402


@dataclass
class UpstreamStream:
    """An open streaming response plus which deployment actually serves it."""

    stream: Any  # async iterator of LiteLLM ``ModelResponseStream`` chunks
    served_alias: str


class ModelRouter:
    """Builds a LiteLLM Router from the model catalogue."""

    def __init__(self, models: dict[str, ModelSpec]) -> None:
        self.models = models
        # Reverse map so we can tell which alias a response really came from.
        self._alias_by_backend = {spec.backend_model: alias for alias, spec in models.items()}
        self.router = Router(
            model_list=[
                {"model_name": alias, "litellm_params": dict(spec.litellm_params)}
                for alias, spec in models.items()
            ],
            fallbacks=[{alias: spec.fallbacks} for alias, spec in models.items() if spec.fallbacks],
            # No same-model retries: a failing primary goes straight to the
            # fallback, which keeps the tail latency of failures bounded and
            # the tests deterministic.
            num_retries=0,
            # Do not take a deployment out of rotation after failures (LiteLLM
            # "cooldown"). With one deployment per alias a cooldown would turn
            # one transient error into minutes of forced fallback.
            disable_cooldowns=True,
        )

    def chain(self, alias: str) -> list[ModelSpec]:
        """The requested model followed by its fallbacks, in order."""
        spec = self.models[alias]
        return [spec, *(self.models[a] for a in spec.fallbacks)]

    async def open_stream(
        self, alias: str, messages: list[dict[str, Any]], max_tokens: int, **params: Any
    ) -> UpstreamStream:
        """Start a streaming completion, falling back if the primary fails.

        Raises the last upstream exception if every model in the chain fails.
        """
        stream = await self.router.acompletion(
            model=alias,
            messages=messages,
            max_tokens=max_tokens,
            stream=True,
            # Ask the backend to append a usage chunk; billing relies on it.
            stream_options={"include_usage": True},
            **params,
        )
        hidden = getattr(stream, "_hidden_params", None) or {}
        backend = hidden.get("litellm_model_name")
        served = self._alias_by_backend.get(str(backend), alias)
        return UpstreamStream(stream=stream, served_alias=served)
