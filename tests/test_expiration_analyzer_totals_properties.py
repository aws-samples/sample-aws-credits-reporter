"""Property-based tests for expiration tier totals (Requirement 7.4, 7.5).

Property 8: For any tier, ``total_remaining`` equals the sum of the remaining
amounts of exactly its member credits and ``count`` equals the number of
members; an empty tier reports 0 and 0.

The ground truth for "its member credits" is the tier's own ``credits`` list:
``classify`` decides membership, and these properties assert that the reported
aggregates (``total_remaining``, ``count``) are consistent with whatever set of
members the tier ended up holding. A missing ``remainingAmount`` contributes 0
to the sum (design of ``_summarize``), so the expected total is the sum over
members that carry a ``remainingAmount``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from hypothesis import given, strategies as st

from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer, TierSummary
from aws_credits_reporter.models import Credit, CurrencyAmount

# --------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------

# Decimal remaining amounts kept to two places and a bounded range so exact
# currency sums stay meaningful; ``None`` models an absent remainingAmount.
_remaining = st.one_of(
    st.none(),
    st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("1000000"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
)

# endDate spans well before and after any generated snapshot date so credits
# land across the past, both tiers, and the far future.
_end_date = st.one_of(
    st.none(),
    st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)),
)

# creditStatus covers ENABLED, DISABLED, and absent so Inactive credits (which
# are excluded from every tier) are exercised too.
_credit_status = st.sampled_from(["ENABLED", "DISABLED", None])


@st.composite
def _credits(draw: st.DrawFn) -> Credit:
    remaining = draw(_remaining)
    amount = CurrencyAmount(remaining, "USD") if remaining is not None else None
    return Credit(
        creditId=draw(st.one_of(st.none(), st.text(min_size=1, max_size=8))),
        endDate=draw(_end_date),
        remainingAmount=amount,
        creditStatus=draw(_credit_status),
        exhaustDate=draw(st.one_of(st.none(), _end_date)),
    )


_credit_lists = st.lists(_credits(), max_size=25)

_now_utc = st.datetimes(
    min_value=datetime(2021, 1, 1),
    max_value=datetime(2029, 12, 31),
).map(lambda dt: dt.replace(tzinfo=timezone.utc))


def _expected_total(tier: TierSummary) -> Decimal:
    """Sum of remaining amounts over exactly the tier's member credits."""
    return sum(
        (c.remainingAmount.amount for c in tier.credits if c.remainingAmount is not None),
        Decimal("0"),
    )


# --------------------------------------------------------------------------
# Property 8
# --------------------------------------------------------------------------


@given(credits=_credit_lists, now_utc=_now_utc)
def test_tier_totals_match_member_credits(credits: list[Credit], now_utc: datetime) -> None:
    """Property 8 (Requirements 7.4, 7.5).

    For each tier produced by ``classify``, ``count`` equals the number of
    member credits and ``total_remaining`` equals the sum of the remaining
    amounts of exactly those members.

    **Validates: Requirements 7.4, 7.5**
    """
    result = ExpirationAnalyzer(now_utc).classify(credits)

    for tier in (result.this_month, result.next_three_months):
        # count equals the number of members.
        assert tier.count == len(tier.credits)
        # total_remaining equals the sum over exactly the member credits.
        assert tier.total_remaining == _expected_total(tier)


@given(now_utc=_now_utc)
def test_empty_tiers_report_zero_and_zero(now_utc: datetime) -> None:
    """An empty tier reports 0 and 0 (Requirements 7.4, 7.5).

    With no credits, both tiers are empty, so each must report a count of 0 and
    a total_remaining of Decimal("0").

    **Validates: Requirements 7.4, 7.5**
    """
    result = ExpirationAnalyzer(now_utc).classify([])

    for tier in (result.this_month, result.next_three_months):
        assert tier.credits == []
        assert tier.count == 0
        assert tier.total_remaining == Decimal("0")
