"""Seed tenants and their credit accounts from ``config/tenants.yaml``.

Idempotent: existing tenants get their key hash, allowlist and limits updated
(so editing the YAML and restarting applies the change) but their *balance is
never reset* -- re-seeding must not hand out free credits.

Run manually with ``python -m gateway.seed``; the gateway also runs it at
startup when ``SEED_ON_STARTUP=true`` (the docker compose default).
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.auth import hash_key
from gateway.config import Settings, TenantSpec, load_tenants
from gateway.db import create_schema, credit_accounts, credit_transactions, make_engine, tenants


async def seed_tenants(
    engine: AsyncEngine, specs: list[TenantSpec], keys: dict[str, str] | None = None
) -> dict[str, int]:
    """Create or update tenants. ``keys`` maps env-var name -> API key.

    Tenants whose key variable is unset are skipped (with a message), so a
    deployment can enable only some tenants. Returns ``{tenant_name: id}``.
    """
    keys = dict(os.environ) if keys is None else keys
    ids: dict[str, int] = {}
    async with engine.begin() as conn:
        for spec in specs:
            api_key = keys.get(spec.api_key_env)
            if not api_key:
                print(f"[seed] skipping tenant {spec.name}: {spec.api_key_env} is not set")
                continue
            values = {
                "api_key_hash": hash_key(api_key),
                "allowed_models": list(spec.allowed_models),
                "rpm_limit": spec.rpm_limit,
                "daily_token_quota": spec.daily_token_quota,
            }
            tenant_id = await conn.scalar(select(tenants.c.id).where(tenants.c.name == spec.name))
            if tenant_id is None:
                tenant_id = await conn.scalar(
                    insert(tenants).values(name=spec.name, **values).returning(tenants.c.id)
                )
                await conn.execute(
                    insert(credit_accounts).values(
                        tenant_id=tenant_id, balance=spec.initial_credits
                    )
                )
                await conn.execute(
                    insert(credit_transactions).values(
                        tenant_id=tenant_id, kind="topup", amount=spec.initial_credits
                    )
                )
            else:
                await conn.execute(
                    update(tenants).where(tenants.c.id == tenant_id).values(**values)
                )
            ids[spec.name] = int(tenant_id)
    return ids


async def _main() -> None:
    """CLI entry point: create the schema and seed tenants from the environment."""
    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    await create_schema(engine)
    ids = await seed_tenants(engine, load_tenants(settings.tenants_config))
    print(f"[seed] tenants ready: {ids}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
