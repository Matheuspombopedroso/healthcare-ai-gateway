"""Tenant authentication: API key -> tenant record.

The isolation guarantee of the whole gateway starts here. The tenant identity
is derived *only* from the presented API key; no endpoint accepts a tenant id
or name from the client. Every downstream query (Redis keys, ledger rows,
credit balance) is scoped by the ``Tenant.id`` returned from this module, so a
client cannot even express "show me tenant B's usage".
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.db import tenants


@dataclass(frozen=True)
class Tenant:
    """The authenticated caller, as seen by the rest of the gateway."""

    id: int
    name: str
    allowed_models: frozenset[str]
    rpm_limit: int
    daily_token_quota: int


def hash_key(api_key: str) -> str:
    """SHA-256 hex digest of an API key (what is stored in Postgres).

    A plain (unsalted) hash is fine here because API keys are long random
    strings, not human passwords, so dictionary attacks do not apply.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def parse_bearer(header_value: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <key>`` header."""
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def lookup_tenant(engine: AsyncEngine, api_key: str) -> Tenant | None:
    """Return the tenant owning ``api_key``, or ``None`` if the key is unknown."""
    digest = hash_key(api_key)
    async with engine.connect() as conn:
        row = (await conn.execute(select(tenants).where(tenants.c.api_key_hash == digest))).first()
    # The DB lookup is by hash equality; the constant-time compare below is a
    # belt-and-braces check that does not leak timing about partial matches.
    if row is None or not hmac.compare_digest(row.api_key_hash, digest):
        return None
    return Tenant(
        id=row.id,
        name=row.name,
        allowed_models=frozenset(row.allowed_models),
        rpm_limit=row.rpm_limit,
        daily_token_quota=row.daily_token_quota,
    )
