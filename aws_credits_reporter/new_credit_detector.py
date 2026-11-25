"""New-credit detection: diff the current snapshot against the prior one.

The ``NewCreditDetector`` compares the set of ``creditId`` values in the
current ``Snapshot`` against the most recent prior ``Snapshot`` and classifies
any creditId present now but absent before as newly issued. Two degraded modes are handled explicitly:

- no prior snapshot exists: every current credit is the initial baseline and
  no credit is flagged as new;
- the prior snapshot could not be retrieved or read: detection halts, nothing
  is flagged as new, and a comparison error is signalled.

The detector never raises on malformed snapshot content; it relies on the
tolerant parsers in ``aws_credits_reporter.models``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from aws_credits_reporter.aggregation import sum_remaining
from aws_credits_reporter.models import Credit, Snapshot, parse_credits

BASELINE_MESSAGE = (
    "no prior run available for comparison; current credits recorded as initial baseline"
)
COMPARISON_ERROR_MESSAGE = (
    "comparison error: the most recent prior snapshot could not be retrieved or read; "
    "new-credit detection halted"
)
ZERO_NEW_MESSAGE = "zero new credits were found"


@dataclass
class NewCreditResult:
    """Outcome of one new-credit detection pass.

    ``new_credits`` preserves the order of the current snapshot. Credits with
    no ``creditId`` are never classified as new. ``total_new_remaining`` sums
    the ``remainingAmount`` of the new credits, treating a missing amount as
    zero; ``new_remaining_currency`` is the currency of that sum, or ``None``
    when no new credit carried a remaining amount.
    """

    new_credit_ids: set[str] = field(default_factory=set)
    new_credits: list[Credit] = field(default_factory=list)
    total_new_remaining: Decimal = Decimal("0")
    new_remaining_currency: str | None = None
    is_initial_baseline: bool = False
    comparison_error: bool = False
    messages: list[str] = field(default_factory=list)


def credit_ids(credits: list[Credit]) -> set[str]:
    """Return the set of non-missing creditId values in ``credits``."""
    return {credit.creditId for credit in credits if credit.creditId is not None}


class NewCreditDetector:
    """Classifies credits present in the current snapshot but not the prior one."""

    def detect(
        self,
        current: Snapshot,
        prior: Snapshot | None,
        prior_read_ok: bool = True,
    ) -> NewCreditResult:
        """Compare ``current`` against ``prior`` and return a ``NewCreditResult``.

        Precedence: an unreadable prior (``prior_read_ok`` False) wins over a
        missing prior, and both short-circuit the set difference.
        """
        current_credits = parse_credits(current.raw_get_credits)

        if not prior_read_ok:
            return NewCreditResult(
                comparison_error=True,
                messages=[COMPARISON_ERROR_MESSAGE],
            )

        if prior is None:
            return NewCreditResult(
                is_initial_baseline=True,
                messages=[BASELINE_MESSAGE],
            )

        prior_ids = credit_ids(parse_credits(prior.raw_get_credits))
        new_ids = credit_ids(current_credits) - prior_ids
        new_credits = [c for c in current_credits if c.creditId in new_ids]
        total, currency = sum_remaining(new_credits)

        return NewCreditResult(
            new_credit_ids=new_ids,
            new_credits=new_credits,
            total_new_remaining=total,
            new_remaining_currency=currency,
            messages=[] if new_ids else [ZERO_NEW_MESSAGE],
        )
