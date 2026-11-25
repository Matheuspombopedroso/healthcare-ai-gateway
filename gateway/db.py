"""PostgreSQL schema: tenants, prepaid credit accounts and the usage ledger.

Why Postgres (and not Redis) for money: credits are the one piece of state that
must survive restarts and be auditable. Every debit is a row in
``credit_transactions`` written in the *same transaction* as the balance
update, so ``balance == initial + sum(transactions)`` always holds.

Redis is used for the hot, approximate, expiring counters (rate limits, daily
token quotas) -- see ``gateway/limits.py``.

The schema is created with ``metadata.create_all`` at startup. A real service
would use migrations (Alembic); that is listed under future work.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    func,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

metadata = MetaData()

tenants = Table(
    "tenants",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    # Only the SHA-256 of the key is stored; a DB leak does not leak keys.
    Column("api_key_hash", String(64), nullable=False, unique=True),
    Column("allowed_models", JSON, nullable=False),
    Column("rpm_limit", Integer, nullable=False),
    Column("daily_token_quota", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

credit_accounts = Table(
    "credit_accounts",
    metadata,
    Column("tenant_id", Integer, ForeignKey("tenants.id"), primary_key=True),
    # Integer millicredits: no floating point in money.
    Column("balance", BigInteger, nullable=False),
    # Defense in depth: even if application logic had a bug, the database
    # refuses to let a balance go negative.
    CheckConstraint("balance >= 0", name="balance_non_negative"),
)

credit_transactions = Table(
    "credit_transactions",
    metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True),
    Column("tenant_id", Integer, ForeignKey("tenants.id"), nullable=False, index=True),
    Column("kind", String(16), nullable=False),  # "topup" | "usage"
    Column("amount", BigInteger, nullable=False),  # signed millicredits
    Column("request_id", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

usage_events = Table(
    "usage_events",
    metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True),
    Column("tenant_id", Integer, ForeignKey("tenants.id"), nullable=False, index=True),
    Column("request_id", String(64), nullable=False, unique=True),
    Column("requested_model", String(64), nullable=False),
    Column("served_model", String(64), nullable=True),
    Column("fallback_used", Boolean, nullable=False, default=False),
    Column("status", String(16), nullable=False),  # "ok" | "error"
    Column("prompt_tokens", Integer, nullable=False, default=0),
    Column("completion_tokens", Integer, nullable=False, default=0),
    Column("cost", BigInteger, nullable=False, default=0),  # millicredits
    Column("ttft_ms", Float, nullable=True),
    Column("latency_ms", Float, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)


def make_engine(database_url: str, pool_size: int = 10) -> AsyncEngine:
    """Create the async engine. ``pool_pre_ping`` survives DB restarts."""
    # A modest pool: every request holds a connection only for a few short
    # statements, and requests beyond the pool wait in a queue instead of
    # opening a burst of new connections (bursts are what overwhelm Postgres
    # -- and, on Docker Desktop for Windows, its port forwarder).
    return create_async_engine(
        database_url, pool_size=pool_size, max_overflow=0, pool_pre_ping=True, pool_timeout=60
    )


async def create_schema(engine: AsyncEngine) -> None:
    """Create tables if they do not exist (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
