"""Prepaid credits: pricing, reservation and settlement in PostgreSQL.

The billing flow for one request is::

    reserve(worst_case_cost)  ->  call model  ->  settle(actual_cost)
                                       \\-> on error: refund(reserved)

**Why reserve first.** If we only debited *after* the response, a tenant with
1 credit left could fire 100 concurrent requests and all of them would be
served before the first debit lands. The reservation is a single conditional
``UPDATE ... WHERE balance >= :amount`` -- Postgres row locking serialises
concurrent reservations on the same account, so the balance can never be
over-committed.

**Why the reservation is a true upper bound.** Worst case = every prompt
character is a token (a BPE token is at least one character, plus a small
per-message overhead) and the model writes exactly ``max_tokens`` tokens, priced
at the most expensive model in the fallback chain. Settlement therefore only
ever *returns* money, and the balance never goes below zero.

All amounts are integer millicredits.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.config import ModelSpec
from gateway.db import credit_accounts, credit_transactions, usage_events

# Tokens added per chat message for role/formatting overhead in the estimate.
PER_MESSAGE_OVERHEAD_TOKENS = 8


def ceil_div(a: int, b: int) -> int:
    """Integer ceiling division (always round charges up, never down)."""
    return -(-a // b)


def cost_of(spec: ModelSpec, prompt_tokens: int, completion_tokens: int) -> int:
    """Price of a request in millicredits, rounded up."""
    return ceil_div(
        prompt_tokens * spec.price_in_per_1k + completion_tokens * spec.price_out_per_1k, 1000
    )


def estimate_prompt_tokens(messages: list[dict[str, object]]) -> int:
    """Upper bound on prompt tokens: characters + per-message overhead."""
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    return chars + PER_MESSAGE_OVERHEAD_TOKENS * len(messages)


def worst_case_cost(chain: list[ModelSpec], prompt_tokens_bound: int, max_tokens: int) -> int:
    """Highest possible cost across the primary and all its fallbacks."""
    return max(cost_of(s, prompt_tokens_bound, max_tokens) for s in chain)


@dataclass
class UsageRecord:
    """Everything the ledger stores about one finished request."""

    request_id: str
    requested_model: str
    served_model: str | None
    fallback_used: bool
    status: str
    prompt_tokens: int
    completion_tokens: int
    cost: int
    ttft_ms: float | None
    latency_ms: float | None


class CreditLedger:
    """All reads and writes of credits and usage go through this class."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Keep the async engine; every method opens its own short transaction."""
        self.engine = engine

    async def reserve(self, tenant_id: int, amount: int) -> bool:
        """Atomically hold ``amount`` from the tenant's balance.

        Returns ``False`` (and changes nothing) if the balance is insufficient.
        """
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(credit_accounts)
                .where(credit_accounts.c.tenant_id == tenant_id)
                .where(credit_accounts.c.balance >= amount)
                .values(balance=credit_accounts.c.balance - amount)
                .returning(credit_accounts.c.balance)
            )
            return result.first() is not None

    async def settle(self, tenant_id: int, reserved: int, record: UsageRecord) -> None:
        """Turn a reservation into the actual charge and write the ledger rows.

        Balance update, usage row and credit transaction are one DB
        transaction: either all are recorded or none is.
        """
        refund = reserved - record.cost
        async with self.engine.begin() as conn:
            if refund:
                await conn.execute(
                    update(credit_accounts)
                    .where(credit_accounts.c.tenant_id == tenant_id)
                    .values(balance=credit_accounts.c.balance + refund)
                )
            await conn.execute(insert(usage_events).values(tenant_id=tenant_id, **record.__dict__))
            if record.cost:
                await conn.execute(
                    insert(credit_transactions).values(
                        tenant_id=tenant_id,
                        kind="usage",
                        amount=-record.cost,
                        request_id=record.request_id,
                    )
                )

    async def balance(self, tenant_id: int) -> int:
        """Current balance in millicredits."""
        async with self.engine.connect() as conn:
            value = await conn.scalar(
                select(credit_accounts.c.balance).where(credit_accounts.c.tenant_id == tenant_id)
            )
        return int(value or 0)

    async def topup(self, tenant_id: int, amount: int) -> int:
        """Add prepaid credits (admin operation). Returns the new balance."""
        async with self.engine.begin() as conn:
            new_balance = await conn.scalar(
                update(credit_accounts)
                .where(credit_accounts.c.tenant_id == tenant_id)
                .values(balance=credit_accounts.c.balance + amount)
                .returning(credit_accounts.c.balance)
            )
            await conn.execute(
                insert(credit_transactions).values(tenant_id=tenant_id, kind="topup", amount=amount)
            )
        return int(new_balance)

    async def usage_summary(self, tenant_id: int, limit: int = 20) -> dict[str, object]:
        """Aggregate and recent usage for ONE tenant (the caller).

        The ``tenant_id`` filter is the isolation boundary: it always comes
        from the authenticated key, never from the request.
        """
        async with self.engine.connect() as conn:
            agg = (
                await conn.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(usage_events.c.prompt_tokens), 0),
                        func.coalesce(func.sum(usage_events.c.completion_tokens), 0),
                        func.coalesce(func.sum(usage_events.c.cost), 0),
                    ).where(usage_events.c.tenant_id == tenant_id)
                )
            ).one()
            rows = (
                await conn.execute(
                    select(usage_events)
                    .where(usage_events.c.tenant_id == tenant_id)
                    .order_by(usage_events.c.id.desc())
                    .limit(limit)
                )
            ).mappings()
            recent = [
                {
                    k: (v.isoformat() if hasattr(v, "isoformat") else v)
                    for k, v in r.items()
                    if k not in ("id", "tenant_id")
                }
                for r in rows
            ]
        return {
            "requests": int(agg[0]),
            "prompt_tokens": int(agg[1]),
            "completion_tokens": int(agg[2]),
            "cost_millicredits": int(agg[3]),
            "recent": recent,
        }
