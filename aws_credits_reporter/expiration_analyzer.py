"""Expiration risk tier classification for AWS Credits Reporter.

Credits are assigned to at most one of two tiers based on their ``endDate``,
evaluated against the current UTC calendar month:

* ``Expiring_This_Month``: ``first_of_current <= endDate <= last_of_current``
* ``Expiring_Next_Three_Months``:
  ``last_of_current < endDate <= last_of_third_following``

Credits without an ``endDate`` are left unclassified. Each tier reports the
sum of remaining amounts (a missing ``remainingAmount`` counts as 0) and the
integer number of credits, reporting 0 and 0 when the tier is empty.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .aggregation import TierSummary, summarize
from .models import Credit, LifecycleStatus, lifecycle_status


@dataclass(frozen=True)
class ExpirationWindow:
    """The UTC calendar-month boundaries used for tier classification."""

    first_of_current: date
    last_of_current: date
    last_of_third_following: date


@dataclass
class ExpirationResult:
    this_month: TierSummary
    next_three_months: TierSummary
    window: ExpirationWindow


def _to_utc_date(now_utc: datetime) -> date:
    """Return the UTC calendar date of ``now_utc``.

    Tz-aware values are converted to UTC first; naive values are treated as
    already being in UTC.
    """
    if now_utc.tzinfo is not None and now_utc.utcoffset() is not None:
        now_utc = now_utc.astimezone(timezone.utc)
    return now_utc.date()


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Advance ``(year, month)`` by ``delta`` months, rolling over the year."""
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _last_day_of(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def compute_window(now_utc: datetime) -> ExpirationWindow:
    """Compute the classification window for the UTC month containing ``now_utc``."""
    today = _to_utc_date(now_utc)
    first_of_current = today.replace(day=1)
    last_of_current = _last_day_of(today.year, today.month)
    third_year, third_month = _add_months(today.year, today.month, 3)
    return ExpirationWindow(
        first_of_current=first_of_current,
        last_of_current=last_of_current,
        last_of_third_following=_last_day_of(third_year, third_month),
    )


class ExpirationAnalyzer:
    """Classifies credits into expiration risk tiers relative to ``now_utc``."""

    def __init__(self, now_utc: datetime):
        self.now_utc = now_utc
        self.as_of_date = _to_utc_date(now_utc)
        self.window = compute_window(now_utc)

    def classify(self, credits: list[Credit]) -> ExpirationResult:
        """Assign each Active_Credit to at most one tier, preserving input order.

        Credits whose ``endDate`` is missing, already in the past relative to
        the current month, or beyond the third following month are not
        placed in any tier. Inactive_Credits (Expired, Exhausted, Disabled at
        the Snapshot_Date) are never assigned to a tier.
        """
        window = self.window
        this_month: list[Credit] = []
        next_three: list[Credit] = []
        for credit in credits:
            end = credit.endDate
            if end is None:
                continue
            if lifecycle_status(credit, self.as_of_date) is not LifecycleStatus.ACTIVE:
                continue  # Inactive_Credits never enter a tier.
            if window.first_of_current <= end <= window.last_of_current:
                this_month.append(credit)
            elif window.last_of_current < end <= window.last_of_third_following:
                next_three.append(credit)
        return ExpirationResult(
            this_month=summarize(this_month),
            next_three_months=summarize(next_three),
            window=window,
        )
