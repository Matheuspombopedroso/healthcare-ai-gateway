"""Prometheus metrics exposed at ``GET /metrics``.

Each ``GatewayMetrics`` instance owns its own ``CollectorRegistry`` instead of
using the global default registry. That lets tests create several apps in one
process without "duplicated timeseries" errors.

Label choice: ``tenant`` is a label because the number of tenants is small and
known (a few products). In a system with thousands of tenants this would blow
up Prometheus cardinality and per-tenant numbers would come from the Postgres
ledger instead.

Latency percentiles (p50/p95) are computed server-side by Prometheus with
``histogram_quantile`` over these histograms; the benchmark script computes
them client-side from raw samples.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

# Buckets tuned for LLM latencies: TTFT is usually tens to hundreds of ms,
# full responses can take many seconds.
_TTFT_BUCKETS = (0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 1, 2, 5)
_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 10, 30)


class GatewayMetrics:
    """All gateway metrics, registered on a private registry."""

    def __init__(self) -> None:
        """Create every metric on a fresh private registry."""
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "gateway_requests_total",
            "Chat requests by tenant, requested model and outcome",
            ["tenant", "model", "status"],
            registry=self.registry,
        )
        self.rejections = Counter(
            "gateway_rejections_total",
            "Requests rejected before reaching a model, by reason",
            ["tenant", "reason"],
            registry=self.registry,
        )
        self.fallbacks = Counter(
            "gateway_fallbacks_total",
            "Requests served by a fallback model instead of the requested one",
            ["requested_model", "served_model"],
            registry=self.registry,
        )
        self.tokens = Counter(
            "gateway_tokens_total",
            "Tokens processed, by tenant, served model and kind (prompt/completion)",
            ["tenant", "model", "kind"],
            registry=self.registry,
        )
        self.credits_spent = Counter(
            "gateway_credits_spent_millicredits_total",
            "Millicredits debited, by tenant",
            ["tenant"],
            registry=self.registry,
        )
        self.ttft = Histogram(
            "gateway_ttft_seconds",
            "Time from request received to first content token sent",
            ["model"],
            buckets=_TTFT_BUCKETS,
            registry=self.registry,
        )
        self.latency = Histogram(
            "gateway_request_latency_seconds",
            "Total time from request received to last token sent",
            ["model"],
            buckets=_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.tokens_per_second = Histogram(
            "gateway_completion_tokens_per_second",
            "Per-request decode throughput: completion tokens / (latency - TTFT)",
            ["model"],
            buckets=(5, 10, 25, 50, 75, 100, 150, 200, 300, 500, 1000),
            registry=self.registry,
        )

    def render(self) -> bytes:
        """Serialize the registry in the Prometheus text format."""
        return generate_latest(self.registry)
