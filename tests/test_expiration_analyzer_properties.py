"""Property-based tests for ``aws_credits_reporter.expiration_analyzer`` (Requirement 7).

Property 7 (Requirements 7.1, 7.2, 7.3): for any credits and any ``now_utc``,
no credit appears in both tiers, and a credit appears in a tier if and only if
its ``endDate`` falls in that tier's window.

``ExpirationAnalyzer.classify`` only assigns a credit to a tier when the credit
is Active at the Snapshot_Date (Req 7.3 / 7.9), so the "if and only if" part of
the property is exercised with Active credits: each generated credit carries a
present ``remainingAmount`` greater than zero, no ``exhaustDate``, and
``creditStatus == "ENABLED"``, so its Lifecycle_Status is driven purely by
``endDate`` relative to the Snapshot_Date. The mutual-exclusivity half of the
property (no credit in both tiers) is additionally checked against arbitrary
credits, including ones that are Inactive and therefore never tiered.

The tier windows are recomputed here directly from ``now_utc`` via
``compute_window`` (the module's own boundary helper), so the test asserts the
classification against the same UTC calendar-month arithmetic the requirement
defines rather than reimplementing month math.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from hypothesis import given, settings, strategies as st

from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer, compute_window
from aws_credits_reporter.models import (
    Credit,
    CurrencyAmount,
    LifecycleStatus,
    lifecycle_status,
)

# --------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------

# A wide but bounded date range so endDates land before, inside, and well
# beyond every tier window the generated ``now_utc`` values can produce.
_dates = st.dates(min_value=date(2019, 1, 1), max_value=date(2031, 12, 31))

# ``now_utc`` as a tz-aware UTC datetime across a range fully contained in the
# ``_dates`` span, so tier windows always have both past and future endDates to
# separate. A fixed noon time keeps the focus on calendar-month boundaries.
_now_utc = st.datetimes(
    min_value=datetime(2022, 1, 1, 12),
    max_value=datetime(2028, 12, 31, 12),
).map(lambda dt: dt.replace(tzinfo=timezone.utc))

# Distinct credit ids so the two tiers' id sets can be compared unambiguously.
_credit_ids = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-", min_size=1, max_size=8  # pragma: allowlist secret
)


@st.composite
def _active_credit(draw: st.DrawFn, credit_id: str) -> Credit:
    """An Active credit whose Lifecycle_Status depends only on ``endDate``.

    Present positive ``remainingAmount``, no ``exhaustDate``, and an ENABLED
    ``creditStatus`` mean rules (b) and (c) of ``lifecycle_status`` never fire,
    so the credit is Active unless its ``endDate`` is strictly before the
    Snapshot_Date (rule (a)).
    """
    amount = draw(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1000000"), places=2))
    return Credit(
        creditId=credit_id,
        endDate=draw(st.one_of(st.none(), _dates)),
        remainingAmount=CurrencyAmount(amount, "USD"),
        creditStatus="ENABLED",
    )


@st.composite
def _arbitrary_credit(draw: st.DrawFn, credit_id: str) -> Credit:
    """A credit with freely varied lifecycle-affecting fields.

    Used to check mutual exclusivity holds even for Inactive credits (which are
    never tiered) and for credits with missing amounts or dates.
    """
    remaining = draw(
        st.one_of(
            st.none(),
            st.decimals(min_value=Decimal("0"), max_value=Decimal("1000000"), places=2),
        )
    )
    return Credit(
        creditId=credit_id,
        endDate=draw(st.one_of(st.none(), _dates)),
        remainingAmount=None if remaining is None else CurrencyAmount(remaining, "USD"),
        exhaustDate=draw(st.one_of(st.none(), _dates)),
        creditStatus=draw(st.sampled_from(["ENABLED", "DISABLED", None])),
    )


def _credits(credit_strategy) -> st.SearchStrategy[list[Credit]]:
    """A list of credits with unique ids, each built by ``credit_strategy``."""
    return st.lists(_credit_ids, unique=True, max_size=12).flatmap(
        lambda ids: st.tuples(*[credit_strategy(cid) for cid in ids]).map(list)
    )


# --------------------------------------------------------------------------
# Property 7
# --------------------------------------------------------------------------


@settings(max_examples=300)
@given(now_utc=_now_utc, credits=_credits(_active_credit))
def test_tier_membership_matches_window_and_is_exclusive(
    now_utc: datetime, credits: list[Credit]
) -> None:
    """Property 7 (Requirements 7.1, 7.2, 7.3).

    For any Active credits and any ``now_utc``: the two tiers are disjoint, and
    a credit is in a tier if and only if it is Active and its ``endDate`` lands
    in that tier's UTC calendar-month window.

    The Active gate matters at the lower boundary: a credit whose ``endDate``
    equals the current month's first day but is strictly before the
    Snapshot_Date is Expired (rule (a) of ``lifecycle_status``), so it is not
    tiered even though its ``endDate`` is inside the current-month window.
    Membership therefore requires ``endDate >= Snapshot_Date`` as well.

    **Validates: Requirements 7.1, 7.2, 7.3**
    """
    analyzer = ExpirationAnalyzer(now_utc)
    window = compute_window(now_utc)
    as_of = analyzer.as_of_date
    result = analyzer.classify(credits)

    this_ids = set(result.this_month.credit_ids)
    next_ids = set(result.next_three_months.credit_ids)

    # Req 7.3: no credit appears in both tiers.
    assert this_ids.isdisjoint(next_ids)

    for credit in credits:
        end = credit.endDate
        cid = credit.creditId
        # These credits are Active exactly when endDate is on or after the
        # Snapshot_Date (rules (b) and (c) are disabled by construction).
        active = end is not None and end >= as_of
        in_this = active and window.first_of_current <= end <= window.last_of_current
        in_next = (
            active and window.last_of_current < end <= window.last_of_third_following
        )

        assert (cid in this_ids) == in_this, (
            f"{cid} endDate={end} window={window} expected this_month={in_this}"
        )
        assert (cid in next_ids) == in_next, (
            f"{cid} endDate={end} window={window} expected next_three={in_next}"
        )


@settings(max_examples=300)
@given(now_utc=_now_utc, credits=_credits(_arbitrary_credit))
def test_tiers_disjoint_and_only_active_credits_tiered(
    now_utc: datetime, credits: list[Credit]
) -> None:
    """Property 7 mutual-exclusivity half over arbitrary credits.

    No credit is ever placed in both tiers, and no Inactive credit (Expired,
    Exhausted, Disabled at the Snapshot_Date) is placed in any tier (Req 7.3).

    **Validates: Requirements 7.1, 7.2, 7.3**
    """
    analyzer = ExpirationAnalyzer(now_utc)
    result = analyzer.classify(credits)

    this_ids = set(result.this_month.credit_ids)
    next_ids = set(result.next_three_months.credit_ids)

    # No credit in both tiers (Req 7.3).
    assert this_ids.isdisjoint(next_ids)

    tiered = this_ids | next_ids
    for credit in credits:
        if credit.creditId in tiered:
            # Only Active credits may be tiered (Req 7.3, 7.9).
            assert lifecycle_status(credit, analyzer.as_of_date) is LifecycleStatus.ACTIVE
