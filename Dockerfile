# One image for the gateway, the mock backend and the benchmark client: they
# share the same Python dependencies, and building once keeps `docker compose
# up` fast. Each compose service just runs a different command.
FROM python:3.12-slim

# No .pyc files in the image, unbuffered logs (so `docker compose logs` is live),
# and LiteLLM uses its bundled price map instead of fetching it at import.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, source second: editing code does not invalidate the
# (slow) dependency layer.
COPY pyproject.toml README.md ./
RUN mkdir gateway mock_backend && touch gateway/__init__.py mock_backend/__init__.py \
    && pip install . \
    && pip uninstall -y healthcare-ai-gateway

COPY gateway ./gateway
COPY mock_backend ./mock_backend
COPY config ./config
COPY bench ./bench
COPY scripts ./scripts

# Run as an unprivileged user.
RUN useradd --create-home appuser
USER appuser

EXPOSE 8400
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8400"]
