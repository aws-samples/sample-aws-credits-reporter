"""Tests for ``aws_credits_reporter.lifecycle_analyzer`` (Task 16.1).

Covers Requirements 4.7, 8.1 to 8.5:

* ``LifecycleAnalyzer.partition`` derives one Lifecycle_Status per credit at
  the Snapshot_Date and splits the input into ``active`` (Active) and
  ``inactive`` (Expired, Exhausted, Disabled), preserving input order in both
  lists. ``status_by_id`` carries the derived status for credits that have a
  ``creditId``.
* Unit tests exercise the three lifecycle fixtures added for this phase
  (expired-but-ENABLED, exhausted with ``remainingAmount == 0``, and DISABLED)
  plus a happy Active credit.
* Property test (via ``hypothesis``): for any list of credits and any
  ``as_of`` date, every credit lands in exactly one partition;
  ``status_by_id`` agrees with the classifier for credits carrying an id;
  and ``len(active) + len(inactive) == len(credits)``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from hypothesis import given, strategies as st

from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer, LifecycleResult
from aws_credits_reporter.models import (
    Credit,
    CurrencyAmount,
    LifecycleStatus,
    lifecycle_status,
    parse_credits,
)

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_DATE = date(2026, 3, 12)


def _load(name: str) -> list[Credit]:
    page = json.loads((FIXTURES / name).read_text())
    return parse_credits([page])


# --------------------------------------------------------------------------
# LifecycleResult shape
# --------------------------------------------------------------------------


def test_result_is_frozen_and_carries_the_snapshot_date() -> None:
    result = LifecycleAnalyzer(SNAPSHOT_DATE).partition([])
    assert isinstance(result, LifecycleResult)
    assert result.snapshot_date == SNAPSHOT_DATE
    assert result.active == []
    assert result.inactive == []
    assert result.status_by_id == {}


# --------------------------------------------------------------------------
# status_of is a thin wrapper over lifecycle_status(credit, snapshot_date)
# --------------------------------------------------------------------------


def test_status_of_matches_the_pure_function() -> None:
    analyzer = LifecycleAnalyzer(SNAPSHOT_DATE)
    credit = Credit(
        creditId="c",
        endDate=date(2025, 1, 1),  # strictly before SNAPSHOT_DATE -> Expired.
    )
    assert analyzer.status_of(credit) == lifecycle_status(credit, SNAPSHOT_DATE)
    assert analyzer.status_of(credit) is LifecycleStatus.EXPIRED


# --------------------------------------------------------------------------
# Fixture-driven unit tests: one credit per lifecycle bucket
# --------------------------------------------------------------------------


def test_expired_but_enabled_credit_lands_in_inactive_with_status_expired() -> None:
    [credit] = _load("get_credits_expired_enabled.json")
    result = LifecycleAnalyzer(SNAPSHOT_DATE).partition([credit])

    assert result.active == []
    assert result.inactive == [credit]
    assert result.status_by_id == {"credit-expired-enabled": LifecycleStatus.EXPIRED}


def test_exhausted_credit_with_zero_remaining_is_exhausted() -> None:
    [credit] = _load("get_credits_exhausted.json")
    result = LifecycleAnalyzer(SNAPSHOT_DATE).partition([credit])

    assert result.active == []
    assert result.inactive == [credit]
    assert result.status_by_id == {"credit-exhausted": LifecycleStatus.EXHAUSTED}


def test_disabled_credit_is_disabled_even_with_future_end_date_and_balance() -> None:
    [credit] = _load("get_credits_disabled.json")
    result = LifecycleAnalyzer(SNAPSHOT_DATE).partition([credit])

    assert result.active == []
    assert result.inactive == [credit]
    assert result.status_by_id == {"credit-disabled": LifecycleStatus.DISABLED}


def test_happy_active_credit_stays_active() -> None:
    credit = Credit(
        creditId="credit-active",
        endDate=date(2027, 1, 1),
        remainingAmount=CurrencyAmount(Decimal("100"), "USD"),
        creditStatus="ENABLED",
    )
    result = LifecycleAnalyzer(SNAPSHOT_DATE).partition([credit])

    assert result.active == [credit]
    assert result.inactive == []
    assert result.status_by_id == {"credit-active": LifecycleStatus.ACTIVE}


def test_partition_preserves_input_order_across_active_and_inactive() -> None:
    expired = _load("get_credits_expired_enabled.json")[0]
    exhausted = _load("get_credits_exhausted.json")[0]
    disabled = _load("get_credits_disabled.json")[0]
    active_one = Credit(creditId="active-1", endDate=date(2027, 1, 1))
    active_two = Credit(creditId="active-2", endDate=date(2028, 1, 1))

    credits = [active_one, expired, active_two, exhausted, disabled]
    result = LifecycleAnalyzer(SNAPSHOT_DATE).partition(credits)

    assert [c.creditId for c in result.active] == ["active-1", "active-2"]
    assert [c.creditId for c in result.inactive] == [
        "credit-expired-enabled",
        "credit-exhausted",
        "credit-disabled",
    ]
    assert result.status_by_id == {
        "active-1": LifecycleStatus.ACTIVE,
        "active-2": LifecycleStatus.ACTIVE,
        "credit-expired-enabled": LifecycleStatus.EXPIRED,
        "credit-exhausted": LifecycleStatus.EXHAUSTED,
        "credit-disabled": LifecycleStatus.DISABLED,
    }


def test_credits_without_credit_id_still_appear_in_partitions_but_not_in_status_by_id() -> None:
    active_no_id = Credit(endDate=date(2027, 1, 1))
    inactive_no_id = Credit(endDate=date(2025, 1, 1))  # strictly before SNAPSHOT_DATE.

    result = LifecycleAnalyzer(SNAPSHOT_DATE).partition([active_no_id, inactive_no_id])

    assert result.active == [active_no_id]
    assert result.inactive == [inactive_no_id]
    assert result.status_by_id == {}


# --------------------------------------------------------------------------
# Property test (Property 9, partial): every credit lands in exactly one
# partition; status_by_id agrees with the classifier for id-bearing credits;
# len(active) + len(inactive) == len(credits).
#
# Validates: Requirements 4.7, 8.2, 8.5
# --------------------------------------------------------------------------


_dates = st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31))
_optional_dates = st.one_of(st.none(), _dates)
_optional_amount = st.one_of(
    st.none(),
    st.decimals(min_value=Decimal("0"), max_value=Decimal("1000"), allow_nan=False, allow_infinity=False, places=2).map(
        lambda amount: CurrencyAmount(Decimal(amount), "USD")
    ),
)
_credit_status = st.sampled_from([None, "ENABLED", "DISABLED"])
_credit_ids = st.one_of(st.none(), st.text(min_size=1, max_size=8))


@st.composite
def _credit(draw) -> Credit:
    return Credit(
        creditId=draw(_credit_ids),
        endDate=draw(_optional_dates),
        exhaustDate=draw(_optional_dates),
        remainingAmount=draw(_optional_amount),
        creditStatus=draw(_credit_status),
    )


@given(credits=st.lists(_credit(), max_size=20), as_of=_dates)
def test_partition_is_total_and_agrees_with_classifier(credits: list[Credit], as_of: date) -> None:
    """Property 9 (partial): partition totality and status_by_id agreement.

    Validates: Requirements 4.7, 8.2, 8.5
    """
    result = LifecycleAnalyzer(as_of).partition(credits)

    # Totality: every credit lands in exactly one partition.
    assert len(result.active) + len(result.inactive) == len(credits)
    active_ids = {id(c) for c in result.active}
    inactive_ids = {id(c) for c in result.inactive}
    assert active_ids.isdisjoint(inactive_ids)
    assert active_ids | inactive_ids == {id(c) for c in credits}

    # Input order is preserved within each partition.
    assert result.active == [c for c in credits if lifecycle_status(c, as_of) is LifecycleStatus.ACTIVE]
    assert result.inactive == [c for c in credits if lifecycle_status(c, as_of) is not LifecycleStatus.ACTIVE]

    # status_by_id agrees with the classifier for every id-bearing credit and
    # never invents entries. Later credits with the same id overwrite earlier
    # ones, which is fine because the classifier is deterministic per credit.
    expected: dict[str, LifecycleStatus] = {}
    for credit in credits:
        if credit.creditId is not None:
            expected[credit.creditId] = lifecycle_status(credit, as_of)
    assert result.status_by_id == expected

    # The Active partition contains only Active credits; the Inactive
    # partition never contains an Active credit.
    for credit in result.active:
        assert lifecycle_status(credit, as_of) is LifecycleStatus.ACTIVE
    for credit in result.inactive:
        assert lifecycle_status(credit, as_of) is not LifecycleStatus.ACTIVE


# --------------------------------------------------------------------------
# Boundary: endDate == as_of stays Active (rule (a) is strict less-than).
# --------------------------------------------------------------------------


def test_end_date_equal_to_snapshot_date_is_active() -> None:
    boundary = Credit(
        creditId="boundary",
        endDate=SNAPSHOT_DATE,
        remainingAmount=CurrencyAmount(Decimal("50"), "USD"),
        creditStatus="ENABLED",
    )
    result = LifecycleAnalyzer(SNAPSHOT_DATE).partition([boundary])

    assert result.active == [boundary]
    assert result.status_by_id == {"boundary": LifecycleStatus.ACTIVE}


def test_snapshot_date_derived_from_tz_aware_datetime_is_not_required_by_the_analyzer() -> None:
    """The analyzer takes a plain date; naive-vs-aware conversion is the caller's job.

    This test just confirms passing a ``date`` computed from a tz-aware
    datetime works as expected, which is how ``Orchestrator.run()`` will
    thread the snapshot timestamp in task 16.5.
    """
    now_utc = datetime(2026, 3, 12, 1, 30, tzinfo=timezone(timedelta(hours=2)))
    snapshot_date = now_utc.astimezone(timezone.utc).date()  # -> 2026-03-11 UTC

    credit = Credit(creditId="edge", endDate=date(2026, 3, 11))  # equal to as_of.
    result = LifecycleAnalyzer(snapshot_date).partition([credit])
    assert result.active == [credit]  # strict less-than, so Active.
