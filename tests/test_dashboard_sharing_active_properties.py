"""Task 26.2: property test for active-only sharing totals in the dashboard.

Task 26.1 scoped the dashboard sharing table to Active_Credits only:
``_sharing_by_option(sharing, lifecycle)`` in ``aws_credits_reporter.dashboard`` now
counts a credit toward a sharing group only when it is an Active_Credit per the
``LifecycleResult`` (its ``creditId`` is ``LifecycleStatus.ACTIVE`` in
``lifecycle.status_by_id``, plus the rare id-less credit that appears by
identity in ``lifecycle.active``). Expired, Exhausted, and Disabled credits
never contribute to a sharing group count or total (Req 22.7, superseding
Req 15.4 for these figures).

Property 17 (Validates: Requirements 22.7):

    For any mix of active and inactive credits run through the real pipeline
    (``LifecycleAnalyzer.partition`` -> ``SharingClassifier.classify`` ->
    ``build_dashboard``):

    * the per-group sharing counts sum to the Active_Credit count
      (``len(lifecycle.active)``), NOT the snapshot total;
    * every credit counted in any sharing group is an Active_Credit;
    * the per-group totals sum to the Active_Credits' total remaining.

Everything runs through the production analysis path so the generated inputs
stay internally consistent. No AWS client is constructed and no mocking library
is used: ``Credit`` and ``CostCategory`` instances are built directly and fed
through ``LifecycleAnalyzer``, ``SharingClassifier``, ``ExpirationAnalyzer``,
``NewCreditDetector`` (a trivial baseline result), and ``build_dashboard``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from aws_credits_reporter.dashboard import SHARING_OPTIONS, build_dashboard
from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
from aws_credits_reporter.models import (
    Credit,
    CurrencyAmount,
    LifecycleStatus,
    lifecycle_status,
)
from aws_credits_reporter.new_credit_detector import NewCreditResult
from aws_credits_reporter.sharing_classifier import SharingClassifier
from aws_credits_reporter.models import CostCategory

# A fixed Snapshot_Date / now so lifecycle derivation is deterministic.
NOW = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
SNAPSHOT_DATE = NOW.date()

# Account ids feeding shareableAccounts; the empty list drives the
# cost-category-based / "Shared by default" branches.
_shareable_accounts = st.lists(
    st.sampled_from([f"acct-{i:03d}" for i in range(6)]),
    max_size=3,
)


@st.composite
def _credit(draw: st.DrawFn) -> Credit:
    """A credit whose fields independently drive sharing and lifecycle.

    ``shareableAccounts`` decides the sharing option; ``endDate``,
    ``exhaustDate``, ``remainingAmount``, and ``creditStatus`` decide the
    Lifecycle_Status, so each generated mix spans Active and all three Inactive
    statuses. ``creditId`` is drawn from a tiny alphabet (``None`` allowed) so
    collisions and id-less credits are both exercised.
    """
    remaining_choice = draw(st.sampled_from([None, "0", "10.00", "250.50"]))
    remaining = (
        CurrencyAmount(Decimal(remaining_choice), "USD")
        if remaining_choice is not None
        else None
    )
    # endDate either absent, in the past (Expired candidate), or future.
    end = draw(
        st.sampled_from(
            [None, date(2024, 1, 1), date(2025, 1, 1), date(2025, 12, 31), date(2026, 6, 1)]
        )
    )
    exhaust = draw(st.sampled_from([None, date(2025, 3, 1)]))
    status = draw(st.sampled_from([None, "ENABLED", "DISABLED"]))
    # ``creditId`` is set by the list builder so ids stay unique (a real
    # creditId is a unique identifier); id-less credits are injected
    # separately by that builder.
    return Credit(
        creditStatus=status,
        remainingAmount=remaining,
        shareableAccounts=draw(_shareable_accounts),
        costCategoryArn=draw(st.sampled_from([None, *_ARNS])),
        ruleName=draw(st.sampled_from([None, "Rule_A"])),
        endDate=end,
        exhaustDate=exhaust,
    )


@st.composite
def _credit_list(draw: st.DrawFn) -> list[Credit]:
    """A mixed list of credits with UNIQUE non-None creditIds plus some id-less.

    ``status_by_id`` is keyed on ``creditId``, so two credits sharing an id
    would collapse to one status and make id-based active membership ambiguous
    (not a scenario real ``get-credits`` output produces, since creditId is a
    unique identifier). Each credit here gets a distinct ``c-<n>`` id, and a
    separately drawn count of credits are left id-less to exercise the
    identity-based active path in ``_sharing_by_option``.
    """
    base = draw(st.lists(_credit(), max_size=14))
    id_less_count = draw(st.integers(min_value=0, max_value=3))
    credits: list[Credit] = []
    for i, credit in enumerate(base):
        credit.creditId = None if i < id_less_count else f"c-{i:03d}"
        credits.append(credit)
    return credits


_ARNS = ["arn:aws:ce::111:costcategory/a", "arn:aws:ce::111:costcategory/b"]


@st.composite
def _category(draw: st.DrawFn) -> CostCategory:
    """A CostCategory reference whose ARN may match a generated credit binding."""
    return CostCategory(
        CostCategoryArn=draw(st.sampled_from(_ARNS)),
        Name=draw(st.sampled_from([None, "Sharing Cat"])),
    )


def _active_total_remaining(active: list[Credit]) -> Decimal:
    """Sum of ``remainingAmount`` over the Active_Credits (missing counts 0)."""
    total = Decimal("0")
    for credit in active:
        if credit.remainingAmount is not None:
            total += credit.remainingAmount.amount
    return total


@settings(max_examples=400)
@given(
    credits=_credit_list(),
    categories=st.lists(_category(), max_size=4),
)
def test_sharing_totals_are_active_only(
    credits: list[Credit], categories: list[CostCategory]
) -> None:
    """Property 17: sharing-group counts sum to the Active_Credit count, every
    grouped credit is Active, and the per-group totals sum to the Active
    credits' total remaining (Req 22.7)."""
    lifecycle = LifecycleAnalyzer(SNAPSHOT_DATE).partition(credits)
    expiration = ExpirationAnalyzer(NOW).classify(credits)
    sharing = SharingClassifier().classify(credits, categories)
    dashboard = build_dashboard(lifecycle, expiration, NewCreditResult(), sharing)

    groups = dashboard.sharing_by_option
    # Both sharing options are always present as keys.
    assert set(groups) == set(SHARING_OPTIONS)

    active = lifecycle.active
    active_ids = {
        cid
        for cid, status in lifecycle.status_by_id.items()
        if status is LifecycleStatus.ACTIVE
    }

    # (1) Per-group counts sum to the Active_Credit count, not the snapshot
    # total. The guard below makes the "not the snapshot total" claim bite:
    # whenever at least one inactive credit exists, the two differ.
    counted = sum(tier.count for tier in groups.values())
    assert counted == len(active)
    if len(credits) != len(active):
        assert counted != len(credits)

    # (2) Every credit counted in any sharing group is an Active_Credit:
    # identified credits are Active in status_by_id; the rare id-less credit
    # appears by identity in lifecycle.active.
    for tier in groups.values():
        for credit in tier.credits:
            assert lifecycle_status(credit, SNAPSHOT_DATE) is LifecycleStatus.ACTIVE
            if credit.creditId is not None:
                assert credit.creditId in active_ids
            else:
                assert any(credit is a for a in active)

    # (3) Per-group totals sum to the Active_Credits' total remaining.
    grouped_total = sum(
        (tier.total_remaining for tier in groups.values()), Decimal("0")
    )
    assert grouped_total == _active_total_remaining(active)


@settings(max_examples=200)
@given(
    credits=_credit_list(),
    categories=st.lists(_category(), max_size=4),
)
def test_no_inactive_credit_contributes_to_sharing(
    credits: list[Credit], categories: list[CostCategory]
) -> None:
    """No Inactive_Credit (Expired, Exhausted, Disabled) is counted in any
    sharing group (Req 22.7)."""
    lifecycle = LifecycleAnalyzer(SNAPSHOT_DATE).partition(credits)
    expiration = ExpirationAnalyzer(NOW).classify(credits)
    sharing = SharingClassifier().classify(credits, categories)
    dashboard = build_dashboard(lifecycle, expiration, NewCreditResult(), sharing)

    inactive = lifecycle.inactive
    for tier in dashboard.sharing_by_option.values():
        for credit in tier.credits:
            assert not any(credit is i for i in inactive)
