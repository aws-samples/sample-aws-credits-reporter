"""Unit tests for ``aws_credits_reporter.expiration_analyzer`` (Requirement 7)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from aws_credits_reporter.expiration_analyzer import (
    ExpirationAnalyzer,
    ExpirationWindow,
    compute_window,
)
from aws_credits_reporter.models import Credit, CurrencyAmount


def _credit(credit_id: str, end: date | None, remaining: str | None = "10") -> Credit:
    amount = CurrencyAmount(Decimal(remaining), "USD") if remaining is not None else None
    return Credit(creditId=credit_id, endDate=end, remainingAmount=amount)


# --------------------------------------------------------------------------
# compute_window
# --------------------------------------------------------------------------


def test_window_mid_year():
    assert compute_window(datetime(2024, 5, 15, tzinfo=timezone.utc)) == ExpirationWindow(
        first_of_current=date(2024, 5, 1),
        last_of_current=date(2024, 5, 31),
        last_of_third_following=date(2024, 8, 31),
    )


def test_window_november_rolls_into_february_with_28_days():
    window = compute_window(datetime(2024, 11, 15, tzinfo=timezone.utc))
    assert window.last_of_current == date(2024, 11, 30)
    assert window.last_of_third_following == date(2025, 2, 28)


def test_window_december_rolls_into_march():
    window = compute_window(datetime(2024, 12, 10, tzinfo=timezone.utc))
    assert window.first_of_current == date(2024, 12, 1)
    assert window.last_of_current == date(2024, 12, 31)
    assert window.last_of_third_following == date(2025, 3, 31)


def test_window_leap_february_current_and_third_following():
    # Current month is a leap February.
    window = compute_window(datetime(2024, 2, 10, tzinfo=timezone.utc))
    assert window.last_of_current == date(2024, 2, 29)
    assert window.last_of_third_following == date(2024, 5, 31)
    # Third following month is a leap February.
    window = compute_window(datetime(2023, 11, 1, tzinfo=timezone.utc))
    assert window.last_of_third_following == date(2024, 2, 29)


def test_window_naive_datetime_treated_as_utc():
    assert compute_window(datetime(2024, 5, 15)) == compute_window(
        datetime(2024, 5, 15, tzinfo=timezone.utc)
    )


def test_window_tz_aware_converted_to_utc_before_taking_month():
    # 2024-05-31 23:30 at UTC-2 is 2024-06-01 01:30 UTC, so the month is June.
    local = datetime(2024, 5, 31, 23, 30, tzinfo=timezone(timedelta(hours=-2)))
    assert compute_window(local).first_of_current == date(2024, 6, 1)


# --------------------------------------------------------------------------
# classify: tier boundaries
# --------------------------------------------------------------------------

# NOW is fixed at the first day of the month so the boundary credit with
# ``endDate == first_of_current`` is not Expired by Req 4.7(a) (endDate <
# as_of); it stays Active and lands in the this-month tier per Req 7.1.
NOW = datetime(2024, 5, 1, 12, tzinfo=timezone.utc)


def _classify(*credits: Credit):
    return ExpirationAnalyzer(NOW).classify(list(credits))


def test_boundaries_of_this_month_tier():
    # ``before`` is Expired (endDate < as_of, Req 4.7(a)) and never enters a
    # tier (Req 7.3); the three boundary credits stay Active.
    result = _classify(
        _credit("before", date(2024, 4, 30)),
        _credit("first", date(2024, 5, 1)),
        _credit("mid", date(2024, 5, 15)),
        _credit("last", date(2024, 5, 31)),
    )
    assert result.this_month.credit_ids == ["first", "mid", "last"]
    assert result.next_three_months.credit_ids == []


def test_boundaries_of_next_three_months_tier():
    result = _classify(
        _credit("last-current", date(2024, 5, 31)),
        _credit("first-next", date(2024, 6, 1)),
        _credit("last-third", date(2024, 8, 31)),
        _credit("first-fourth", date(2024, 9, 1)),
    )
    assert result.this_month.credit_ids == ["last-current"]
    assert result.next_three_months.credit_ids == ["first-next", "last-third"]


def test_each_credit_in_at_most_one_tier_and_order_preserved():
    credits = [
        _credit("c", date(2024, 7, 4)),
        _credit("a", date(2024, 5, 2)),
        _credit("d", date(2024, 6, 30)),
        _credit("b", date(2024, 5, 30)),
        _credit("far", date(2025, 1, 1)),
        _credit("past", date(2023, 12, 31)),
    ]
    result = ExpirationAnalyzer(NOW).classify(credits)
    assert result.this_month.credit_ids == ["a", "b"]
    assert result.next_three_months.credit_ids == ["c", "d"]
    assert not set(result.this_month.credit_ids) & set(result.next_three_months.credit_ids)
    assert result.window == compute_window(NOW)


def test_year_rollover_classification():
    analyzer = ExpirationAnalyzer(datetime(2024, 12, 10, tzinfo=timezone.utc))
    result = analyzer.classify(
        [
            _credit("dec", date(2024, 12, 31)),
            _credit("jan", date(2025, 1, 1)),
            _credit("mar", date(2025, 3, 31)),
            _credit("apr", date(2025, 4, 1)),
        ]
    )
    assert result.this_month.credit_ids == ["dec"]
    assert result.next_three_months.credit_ids == ["jan", "mar"]


# --------------------------------------------------------------------------
# classify: summaries
# --------------------------------------------------------------------------


def test_empty_tiers_report_zero_and_zero():
    result = _classify()
    for tier in (result.this_month, result.next_three_months):
        assert tier.count == 0
        assert tier.total_remaining == Decimal("0")
        assert tier.credit_ids == []
        assert tier.credits == []
        assert tier.currency is None


def test_tier_sum_and_count():
    result = _classify(
        _credit("a", date(2024, 5, 3), "100.25"),
        _credit("b", date(2024, 5, 20), "0.75"),
        _credit("c", date(2024, 7, 1), "42"),
    )
    assert result.this_month.count == 2
    assert result.this_month.total_remaining == Decimal("101.00")
    assert result.this_month.currency == "USD"
    assert result.next_three_months.count == 1
    assert result.next_three_months.total_remaining == Decimal("42")
    assert [c.creditId for c in result.this_month.credits] == ["a", "b"]


def test_missing_remaining_amount_counts_as_zero_but_still_counted():
    result = _classify(
        _credit("a", date(2024, 5, 3), "50"),
        _credit("b", date(2024, 5, 4), None),
    )
    assert result.this_month.count == 2
    assert result.this_month.total_remaining == Decimal("50")


def test_missing_end_date_is_unclassified():
    result = _classify(_credit("no-end", None, "999"))
    assert result.this_month.count == 0
    assert result.next_three_months.count == 0
    assert result.this_month.total_remaining == Decimal("0")


def test_credit_without_id_is_counted_but_omitted_from_ids():
    result = _classify(Credit(endDate=date(2024, 5, 10)))
    assert result.this_month.count == 1
    assert result.this_month.credit_ids == []
    assert len(result.this_month.credits) == 1

# --------------------------------------------------------------------------
# Task 11.4: month-boundary edge cases (Requirements 7.1, 7.2)
#
# These tests pin the four calendar boundaries called out by Req 7.1/7.2 and
# the December-to-January year rollover, each exercised with credits that are
# unambiguously Active at the Snapshot_Date: a future endDate relative to the
# tier under test, a present positive remainingAmount, no exhaustDate, and
# creditStatus ENABLED. That keeps the lifecycle derivation (Req 4.7) out of
# the way so the assertions isolate the tier-window logic.
# --------------------------------------------------------------------------


def _active_credit(credit_id: str, end: date) -> Credit:
    """An unambiguously Active credit ending on ``end`` (Req 7 tier candidate)."""
    return Credit(
        creditId=credit_id,
        endDate=end,
        remainingAmount=CurrencyAmount(Decimal("100"), "USD"),
        creditStatus="ENABLED",
        exhaustDate=None,
    )


# Snapshot_Date fixed mid-month for the boundaries where the endDate is on or
# after the 15th; the first-of-current-month boundary needs its own
# Snapshot_Date (see FIRST_OF_MONTH_NOW below) because endDate 2024-05-01 is
# strictly before a mid-month as_of and would be Expired by Req 4.7(a).
MID_MONTH_NOW = datetime(2024, 5, 15, 9, tzinfo=timezone.utc)

# Snapshot_Date fixed at the first day of the month so a credit ending on the
# first day of the current month is on or after the Snapshot_Date and stays
# Active (Req 4.7(a) needs endDate strictly before as_of to mark Expired).
FIRST_OF_MONTH_NOW = datetime(2024, 5, 1, 9, tzinfo=timezone.utc)


def test_boundary_first_day_of_current_month_is_this_month():
    # Req 7.1: endDate == first day of current month -> Expiring_This_Month.
    result = ExpirationAnalyzer(FIRST_OF_MONTH_NOW).classify(
        [_active_credit("first-of-current", date(2024, 5, 1))]
    )
    assert result.this_month.credit_ids == ["first-of-current"]
    assert result.next_three_months.credit_ids == []


def test_boundary_last_day_of_current_month_is_this_month():
    # Req 7.1: endDate == last day of current month -> Expiring_This_Month.
    result = ExpirationAnalyzer(MID_MONTH_NOW).classify(
        [_active_credit("last-of-current", date(2024, 5, 31))]
    )
    assert result.this_month.credit_ids == ["last-of-current"]
    assert result.next_three_months.credit_ids == []


def test_boundary_first_day_of_next_month_is_next_three_months():
    # Req 7.2: endDate == first day of the month after the current month ->
    # Expiring_Next_Three_Months (just past the this-month upper bound).
    result = ExpirationAnalyzer(MID_MONTH_NOW).classify(
        [_active_credit("first-of-next", date(2024, 6, 1))]
    )
    assert result.this_month.credit_ids == []
    assert result.next_three_months.credit_ids == ["first-of-next"]


def test_boundary_last_day_of_third_following_month_is_next_three_months():
    # Req 7.2: endDate == last day of the third calendar month following the
    # current month (Aug for a May Snapshot_Date) -> Expiring_Next_Three_Months.
    result = ExpirationAnalyzer(MID_MONTH_NOW).classify(
        [_active_credit("last-of-third", date(2024, 8, 31))]
    )
    assert result.this_month.credit_ids == []
    assert result.next_three_months.credit_ids == ["last-of-third"]


def test_boundary_first_day_of_fourth_month_is_unclassified():
    # Req 7.2: endDate == first day of the fourth following month (Sep for a
    # May Snapshot_Date) is beyond both tiers -> unclassified.
    result = ExpirationAnalyzer(MID_MONTH_NOW).classify(
        [_active_credit("first-of-fourth", date(2024, 9, 1))]
    )
    assert result.this_month.credit_ids == []
    assert result.next_three_months.credit_ids == []
    assert result.this_month.count == 0
    assert result.next_three_months.count == 0


def test_all_four_boundaries_in_one_run():
    # The four boundary credits together: the two current-month boundaries land
    # in this-month, the next/third boundaries land in next-three, and the
    # fourth-month credit is dropped from both tiers.
    result = ExpirationAnalyzer(FIRST_OF_MONTH_NOW).classify(
        [
            _active_credit("first-of-current", date(2024, 5, 1)),
            _active_credit("last-of-current", date(2024, 5, 31)),
            _active_credit("first-of-next", date(2024, 6, 1)),
            _active_credit("last-of-third", date(2024, 8, 31)),
            _active_credit("first-of-fourth", date(2024, 9, 1)),
        ]
    )
    assert result.this_month.credit_ids == ["first-of-current", "last-of-current"]
    assert result.next_three_months.credit_ids == ["first-of-next", "last-of-third"]


# --- December-to-January rollover -----------------------------------------

# Snapshot_Date in December so the current-month window is December and the
# third following month is the following March, crossing the year boundary.
DECEMBER_NOW = datetime(2024, 12, 15, 9, tzinfo=timezone.utc)


def test_december_boundary_last_day_of_december_is_this_month():
    # Req 7.1: endDate == 31 Dec (last day of current month) -> this-month.
    result = ExpirationAnalyzer(DECEMBER_NOW).classify(
        [_active_credit("dec-31", date(2024, 12, 31))]
    )
    assert result.this_month.credit_ids == ["dec-31"]
    assert result.next_three_months.credit_ids == []


def test_december_to_january_rollover_first_day_of_january_is_next_three_months():
    # Req 7.2: endDate == 1 Jan of the next year (first day of the month after
    # December) -> Expiring_Next_Three_Months across the year boundary.
    result = ExpirationAnalyzer(DECEMBER_NOW).classify(
        [_active_credit("jan-1", date(2025, 1, 1))]
    )
    assert result.this_month.credit_ids == []
    assert result.next_three_months.credit_ids == ["jan-1"]


def test_december_rollover_last_day_of_march_is_next_three_months():
    # Req 7.2: third following month after December is the next March; its last
    # day (31 Mar) is the upper bound of the next-three tier.
    result = ExpirationAnalyzer(DECEMBER_NOW).classify(
        [_active_credit("mar-31", date(2025, 3, 31))]
    )
    assert result.this_month.credit_ids == []
    assert result.next_three_months.credit_ids == ["mar-31"]


def test_december_rollover_first_day_of_april_is_unclassified():
    # Req 7.2: 1 Apr is the first day of the fourth month after December and is
    # beyond both tiers -> unclassified.
    result = ExpirationAnalyzer(DECEMBER_NOW).classify(
        [_active_credit("apr-1", date(2025, 4, 1))]
    )
    assert result.this_month.credit_ids == []
    assert result.next_three_months.credit_ids == []


def test_december_to_january_rollover_full_boundary_set():
    # All December-rollover boundaries in one run: 31 Dec in this-month, 1 Jan
    # and 31 Mar in next-three, 1 Apr dropped.
    result = ExpirationAnalyzer(DECEMBER_NOW).classify(
        [
            _active_credit("dec-31", date(2024, 12, 31)),
            _active_credit("jan-1", date(2025, 1, 1)),
            _active_credit("mar-31", date(2025, 3, 31)),
            _active_credit("apr-1", date(2025, 4, 1)),
        ]
    )
    assert result.this_month.credit_ids == ["dec-31"]
    assert result.next_three_months.credit_ids == ["jan-1", "mar-31"]
    assert result.window.first_of_current == date(2024, 12, 1)
    assert result.window.last_of_current == date(2024, 12, 31)
    assert result.window.last_of_third_following == date(2025, 3, 31)
