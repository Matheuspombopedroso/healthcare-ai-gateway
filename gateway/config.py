"""Configuration loading: environment settings plus the YAML catalogues.

Where it sits: every other module receives a ``Settings`` object (and the
parsed model/tenant catalogues) instead of reading the environment itself.
That keeps the app testable -- tests build a ``Settings`` pointing at their own
Redis database, Postgres and mock server.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Matches ${VAR} placeholders in YAML values.
_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass(frozen=True)
class ModelSpec:
    """One public model alias and how it is served and priced."""

    alias: str
    litellm_params: dict[str, Any]
    price_in_per_1k: int  # millicredits per 1,000 prompt tokens
    price_out_per_1k: int  # millicredits per 1,000 completion tokens
    fallbacks: list[str] = field(default_factory=list)

    @property
    def backend_model(self) -> str:
        """The LiteLLM model string, e.g. ``openai/mock-fast``."""
        return str(self.litellm_params["model"])


@dataclass(frozen=True)
class TenantSpec:
    """Static tenant definition from ``config/tenants.yaml``."""

    name: str
    api_key_env: str
    allowed_models: list[str]
    rpm_limit: int
    daily_token_quota: int
    initial_credits: int


@dataclass(frozen=True)
class Settings:
    """Runtime settings, read from environment variables with local defaults."""

    database_url: str
    redis_url: str
    models_config: Path
    tenants_config: Path
    admin_token: str | None
    default_max_tokens: int = 256
    max_tokens_cap: int = 1024
    seed_on_startup: bool = True
    db_pool_size: int = 10
    redis_socket_timeout: float = 5.0

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the environment (see ``.env.example``)."""
        return cls(
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql+asyncpg://gateway:gateway@localhost:15432/gateway?ssl=disable",
            ),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:16379/0"),
            models_config=Path(os.getenv("MODELS_CONFIG", REPO_ROOT / "config" / "models.yaml")),
            tenants_config=Path(os.getenv("TENANTS_CONFIG", REPO_ROOT / "config" / "tenants.yaml")),
            admin_token=os.getenv("ADMIN_TOKEN") or None,
            default_max_tokens=int(os.getenv("DEFAULT_MAX_TOKENS", "256")),
            max_tokens_cap=int(os.getenv("MAX_TOKENS_CAP", "1024")),
            seed_on_startup=os.getenv("SEED_ON_STARTUP", "true").lower() == "true",
            db_pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
            redis_socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "5")),
        )


def _expand_env(value: Any, env: dict[str, str]) -> Any:
    """Recursively replace ``${VAR}`` in strings with values from ``env``.

    Missing variables raise instead of silently becoming empty strings: a
    blank ``api_base`` would otherwise surface much later as a confusing
    connection error.
    """
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in env:
                raise KeyError(f"environment variable {name} is required by the model config")
            return env[name]

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, env) for v in value]
    return value


def load_models(path: Path, env: dict[str, str] | None = None) -> dict[str, ModelSpec]:
    """Parse the model catalogue into ``{alias: ModelSpec}`` and validate it."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw = _expand_env(raw, dict(os.environ) if env is None else env)
    models = {m["alias"]: ModelSpec(**m) for m in raw["models"]}
    for spec in models.values():
        for fb in spec.fallbacks:
            if fb not in models:
                raise ValueError(f"model {spec.alias} falls back to unknown alias {fb}")
    return models


def load_tenants(path: Path) -> list[TenantSpec]:
    """Parse the tenant catalogue."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [TenantSpec(**t) for t in raw["tenants"]]
