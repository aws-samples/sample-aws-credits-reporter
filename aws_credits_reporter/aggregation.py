"""Shared aggregation helpers over lists of credits.

One implementation of the "sum remaining amounts" and "tier summary" rules used
by the expiration analyzer, new-credit detector, dashboard, and email body, so
the totals in every part of the report are computed the same way.

The currency of a total is the currency of the first credit that carries a
``remainingAmount``; it is ``None`` when none does. A missing
``remainingAmount`` contributes zero. This module is pure: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import Credit


@dataclass
class TierSummary:
    """Aggregate view of a group of credits (count, ids, and remaining total)."""

    credit_ids: list[str] = field(default_factory=list)
    credits: list[Credit] = field(default_factory=list)
    total_remaining: Decimal = Decimal("0")
    count: int = 0
    currency: str | None = None


def sum_remaining(credits: list[Credit]) -> tuple[Decimal, str | None]:
    """``(total, currency)`` over the remaining amounts of ``credits``."""
    total = Decimal("0")
    currency: str | None = None
    for credit in credits:
        amount = credit.remainingAmount
        if amount is None:
            continue
        total += amount.amount
        if currency is None:
            currency = amount.currency
    return total, currency


def summarize(credits: list[Credit]) -> TierSummary:
    """Build a ``TierSummary`` for ``credits``, preserving input order."""
    total, currency = sum_remaining(credits)
    return TierSummary(
        credit_ids=[c.creditId for c in credits if c.creditId is not None],
        credits=list(credits),
        total_remaining=total,
        count=len(credits),
        currency=currency,
    )


def count_label(count: int) -> str:
    """``"1 credit"`` / ``"N credits"``."""
    return f"{count} credit{'' if count == 1 else 's'}"
