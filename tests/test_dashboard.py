"""Unit tests for ``aws_credits_reporter.dashboard``.

Task 12.1 (Requirements 6.3, 6.5, 8.1 to 8.7).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from aws_credits_reporter.dashboard import (
    BASELINE_NOTICE,
    COMPARISON_ERROR_NOTICE,
    Dashboard,
    build_dashboard,
    format_money,
)
from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer, ExpirationResult
from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
from aws_credits_reporter.models import (
    CostCategory,
    Credit,
    CurrencyAmount,
    LifecycleStatus,
    Snapshot,
    parse_credits,
)
from aws_credits_reporter.sharing_classifier import (
    COST_CATEGORY_BASED,
    SHARED_BY_DEFAULT,
    SHARING_DISABLED,
    SharingClassifier,
)
from aws_credits_reporter.new_credit_detector import NewCreditDetector, NewCreditResult

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2025, 1, 10, 12, 0, tzinfo=timezone.utc)
SNAPSHOT_DATE = NOW.date()


def _dashboard(credits, expiration, new_credits, snapshot_date=None):
    """Build a dashboard from raw credits, partitioning at ``snapshot_date``.

    Mirrors the orchestrator: the Lifecycle_Analyzer partitions the credits
    and ``build_dashboard`` consumes the resulting ``LifecycleResult``.
    """
    if snapshot_date is None:
        snapshot_date = SNAPSHOT_DATE
    lifecycle = LifecycleAnalyzer(snapshot_date).partition(credits)
    return build_dashboard(lifecycle, expiration, new_credits)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _snapshot(key: str, *pages: dict) -> Snapshot:
    return Snapshot(
        key=key,
        taken_at=datetime(2025, 1, 10, tzinfo=timezone.utc),
        raw_get_credits=list(pages),
    )


def _fixture_dashboard() -> tuple[Dashboard, list[Credit]]:
    """Current run = page1 + page2; prior run = page1 only.

    With ``NOW`` in January 2025: credit-0001 (endDate 2025-01-14) expires
    this month; nothing expires in Feb to Apr 2025; credit-0003 is new.
    """
    page1, page2 = _load("get_credits_page1.json"), _load("get_credits_page2.json")
    current = _snapshot("snapshots/20250110T000000Z.json", page1, page2)
    prior = _snapshot("snapshots/20250109T000000Z.json", page1)
    credits = parse_credits(current.raw_get_credits)
    expiration = ExpirationAnalyzer(NOW).classify(credits)
    new_credits = NewCreditDetector().detect(current, prior)
    return _dashboard(credits, expiration, new_credits), credits


def _empty_expiration() -> ExpirationResult:
    return ExpirationAnalyzer(NOW).classify([])


def _credit(
    credit_id: str,
    status: str | None = "ENABLED",
    remaining: str | None = "10.00",
    currency: str = "USD",
    end: date | None = None,
) -> Credit:
    amount = CurrencyAmount(Decimal(remaining), currency) if remaining is not None else None
    return Credit(creditId=credit_id, creditStatus=status, remainingAmount=amount, endDate=end)


# --------------------------------------------------------------------------
# format_money (Req 8.2)
# --------------------------------------------------------------------------


def test_format_money_symbols_and_two_decimals():
    assert format_money(Decimal("1234.5"), "USD") == "$1234.50"
    assert format_money(Decimal("0"), "USD") == "$0.00"
    assert format_money(Decimal("99.999"), "EUR") == "\u20ac100.00"
    assert format_money(Decimal("0.005"), "GBP") == "\u00a30.01"
    assert format_money(Decimal("1500"), "JPY") == "\u00a51500.00"


def test_format_money_unknown_currency_uses_iso_prefix_and_none_is_bare():
    assert format_money(Decimal("12"), "CHF") == "CHF 12.00"
    assert format_money(Decimal("12"), None) == "12.00"
    assert format_money(Decimal("7.25"), "usd") == "$7.25"


# --------------------------------------------------------------------------
# build_dashboard (Req 6.3, 6.5, 8.2 to 8.7)
# --------------------------------------------------------------------------


def test_build_dashboard_values_from_fixtures():
    dashboard, _ = _fixture_dashboard()

    # Active_Credits: credit-0001 (7250.50) + credit-0003 (1234.56); 0002 is EXPIRED.
    assert dashboard.active_count == 2
    assert dashboard.active_total == Decimal("8485.06")
    assert dashboard.active_currency == "USD"
    assert dashboard.active_total_display() == "$8485.06"

    # Expiring this month: credit-0001 only.
    assert dashboard.this_month_count == 1
    assert dashboard.this_month_total == Decimal("7250.50")
    assert dashboard.this_month_total_display() == "$7250.50"

    # Nothing expires in the next three months: 0 and 0 (Req 7.5 / 8.4).
    assert dashboard.next_three_count == 0
    assert dashboard.next_three_total == Decimal("0")
    assert dashboard.next_three_total_display() == "0.00"

    # Newly issued: credit-0003 only.
    assert dashboard.new_count == 1
    assert dashboard.new_credit_ids == ["credit-0003"]
    assert dashboard.new_total == Decimal("1234.56")
    assert dashboard.new_total_display() == "$1234.56"

    assert dashboard.is_initial_baseline is False
    assert dashboard.comparison_error is False
    assert dashboard.notices == []


def test_build_dashboard_next_three_months_tier_and_sorted_new_ids():
    credits = [
        _credit("z-credit", remaining="5.00", end=date(2025, 3, 31)),
        _credit("a-credit", remaining="7.50", end=date(2025, 4, 30)),
        _credit("m-credit", status="DISABLED", remaining="100.00", end=date(2025, 2, 1)),
    ]
    expiration = ExpirationAnalyzer(NOW).classify(credits)
    new_credits = NewCreditResult(
        new_credit_ids={"z-credit", "a-credit"},
        new_credits=credits[:2],
        total_new_remaining=Decimal("12.50"),
        new_remaining_currency="USD",
    )
    dashboard = _dashboard(credits, expiration, new_credits)

    # z and a are Active (ENABLED, remaining > 0, endDate in future);
    # m-credit is DISABLED so it is Inactive per Req 4.7(c) and does NOT
    # enter a tier per Req 7.3.
    assert dashboard.active_count == 2
    assert dashboard.active_total == Decimal("12.50")
    assert dashboard.inactive_count == 1
    assert dashboard.this_month_count == 0
    assert dashboard.next_three_count == 2
    assert dashboard.next_three_total == Decimal("12.50")
    assert dashboard.new_count == 2
    assert dashboard.new_credit_ids == ["a-credit", "z-credit"]


def test_build_dashboard_no_active_credits():
    # DISABLED credits are Inactive per Req 4.7(c); a credit with an unknown
    # status but no other Inactive signal (endDate present-and-past, exhaust
    # date, remaining==0) is Active.
    credits = [
        _credit("c1", status="DISABLED"),
        _credit("c2", status="DISABLED"),
    ]
    dashboard = _dashboard(credits, _empty_expiration(), NewCreditResult())
    assert dashboard.active_count == 0
    assert dashboard.active_total == Decimal("0")
    assert dashboard.active_currency is None
    assert dashboard.has_active_credits is False
    assert dashboard.inactive_count == 2


def test_build_dashboard_missing_remaining_counts_as_zero():
    credits = [_credit("c1", remaining=None), _credit("c2", remaining="3.00")]
    dashboard = _dashboard(credits, _empty_expiration(), NewCreditResult())
    assert dashboard.active_count == 2
    assert dashboard.active_total == Decimal("3.00")
    assert dashboard.active_currency == "USD"
    assert dashboard.inactive_count == 0


def test_build_dashboard_baseline_notice():
    current = _snapshot("snapshots/20250110T000000Z.json", _load("get_credits_page1.json"))
    result = NewCreditDetector().detect(current, None)
    dashboard = _dashboard(
        parse_credits(current.raw_get_credits), _empty_expiration(), result
    )
    assert dashboard.is_initial_baseline is True
    assert dashboard.comparison_error is False
    assert dashboard.new_count == 0
    assert dashboard.notices == [BASELINE_NOTICE]


def test_build_dashboard_comparison_error_notice():
    current = _snapshot("snapshots/20250110T000000Z.json", _load("get_credits_page1.json"))
    result = NewCreditDetector().detect(current, None, prior_read_ok=False)
    dashboard = _dashboard(
        parse_credits(current.raw_get_credits), _empty_expiration(), result
    )
    assert dashboard.comparison_error is True
    assert dashboard.new_count == 0
    assert dashboard.notices == [COMPARISON_ERROR_NOTICE]


def test_build_dashboard_active_plus_inactive_equals_total():
    # Mixed lifecycle: Active, Expired (endDate<as_of), Exhausted (remaining=0), Disabled.
    credits = [
        _credit("active", status="ENABLED", remaining="10", end=date(2025, 6, 1)),
        _credit("expired", status="ENABLED", remaining="10", end=date(2024, 1, 1)),
        _credit("exhausted", status="ENABLED", remaining="0", end=date(2025, 6, 1)),
        _credit("disabled", status="DISABLED", remaining="10", end=date(2025, 6, 1)),
    ]
    dashboard = _dashboard(credits, _empty_expiration(), NewCreditResult())
    assert dashboard.active_count == 1
    assert dashboard.inactive_count == 3
    assert dashboard.active_count + dashboard.inactive_count == len(credits)


def test_build_dashboard_inactive_breakdown_by_status():
    # One credit of each inactive status plus one active, so every status
    # carries a non-zero count (Req 8.8).
    credits = [
        _credit("active", remaining="10", end=date(2025, 6, 1)),
        _credit("expired", remaining="400", end=date(2024, 1, 1)),  # Expired
        _credit("exhausted", remaining="0", end=date(2025, 6, 1)),  # Exhausted
        _credit("disabled", status="DISABLED", remaining="2000", end=date(2025, 6, 1)),
    ]
    dashboard = _dashboard(credits, _empty_expiration(), NewCreditResult())

    # Each inactive status has exactly one credit; the Exhausted total is
    # zero by definition of rule (b) (Req 8.8).
    assert dashboard.inactive_by_status[LifecycleStatus.EXPIRED].count == 1
    assert dashboard.inactive_by_status[LifecycleStatus.EXPIRED].total_remaining == Decimal("400")
    assert dashboard.inactive_by_status[LifecycleStatus.EXHAUSTED].count == 1
    assert dashboard.inactive_by_status[LifecycleStatus.EXHAUSTED].total_remaining == Decimal("0")
    assert dashboard.inactive_by_status[LifecycleStatus.DISABLED].count == 1
    assert dashboard.inactive_by_status[LifecycleStatus.DISABLED].total_remaining == Decimal("2000")
    assert dashboard.inactive_status_display(LifecycleStatus.EXPIRED) == (1, "$400.00")
    assert dashboard.inactive_status_display(LifecycleStatus.DISABLED) == (1, "$2000.00")


def test_inactive_by_status_always_has_all_three_keys_at_zero():
    dashboard = _dashboard([], _empty_expiration(), NewCreditResult())
    assert set(dashboard.inactive_by_status) == {
        LifecycleStatus.EXPIRED,
        LifecycleStatus.EXHAUSTED,
        LifecycleStatus.DISABLED,
    }
    for status in dashboard.inactive_by_status:
        tier = dashboard.inactive_by_status[status]
        assert tier.count == 0
        assert tier.total_remaining == Decimal("0")


def test_build_dashboard_inactive_count():
    credits = [
        _credit("active", remaining="10", end=date(2025, 6, 1)),
        _credit("expired", remaining="10", end=date(2024, 1, 1)),  # Expired
        _credit("exhausted", remaining="0", end=date(2025, 6, 1)),  # Exhausted
    ]
    dashboard = _dashboard(credits, _empty_expiration(), NewCreditResult())
    assert dashboard.active_count == 1
    assert dashboard.inactive_count == 2


# --------------------------------------------------------------------------
# Sharing-option figures (task 19.2, Req 14.7, 14.8, 15.4, 15.5)
# --------------------------------------------------------------------------


def _sharing_credit(credit_id: str, remaining="10.00", shareable=None, arn=None, rule=None) -> Credit:
    amount = CurrencyAmount(Decimal(remaining), "USD") if remaining is not None else None
    return Credit(
        creditId=credit_id,
        creditStatus="ENABLED",
        remainingAmount=amount,
        shareableAccounts=list(shareable) if shareable else [],
        costCategoryArn=arn,
        ruleName=rule,
        endDate=date(2025, 6, 1),
    )


_CC_ARN = "arn:aws:ce::111111111111:costcategory/test-cc"


def _sharing_category(name="Credit Sharing", arn=_CC_ARN) -> CostCategory:
    return CostCategory(
        CostCategoryArn=arn,
        Name=name,
        SupportedResourceTypes=["billing:creditsharing"],
    )


def _classify(credits, categories):
    return SharingClassifier().classify(credits, categories)


def _sharing_dashboard(credits, categories):
    """Build a dashboard threading a real SharingResult through build_dashboard."""
    lifecycle = LifecycleAnalyzer(SNAPSHOT_DATE).partition(credits)
    expiration = ExpirationAnalyzer(NOW).classify(credits)
    sharing = _classify(credits, categories)
    return build_dashboard(lifecycle, expiration, NewCreditResult(), sharing)


def test_build_dashboard_sharing_groups_counts_and_totals():
    # One credit with shareableAccounts (Shared by default), one with a full
    # costCategoryArn + ruleName binding (cost-category-based).
    credits = [
        _sharing_credit("acct", remaining="100.00", shareable=["111111111111"]),
        _sharing_credit("cat", remaining="250.00", arn=_CC_ARN, rule="Credit_Rule"),
    ]
    dashboard = _sharing_dashboard(credits, [_sharing_category("Sharing Cat")])

    cost_cat = dashboard.sharing_by_option[COST_CATEGORY_BASED]
    assert cost_cat.count == 1
    assert cost_cat.total_remaining == Decimal("250.00")

    default = dashboard.sharing_by_option[SHARED_BY_DEFAULT]
    assert default.count == 1
    assert default.total_remaining == Decimal("100.00")

    # count + total displayed with currency symbol.
    assert dashboard.sharing_option_display(COST_CATEGORY_BASED) == (1, "$250.00")
    assert dashboard.sharing_option_display(SHARED_BY_DEFAULT) == (1, "$100.00")


def test_build_dashboard_sharing_both_keys_always_present():
    dashboard = _sharing_dashboard([], [])
    assert set(dashboard.sharing_by_option) == {
        COST_CATEGORY_BASED,
        SHARED_BY_DEFAULT,
        SHARING_DISABLED,
    }
    for tier in dashboard.sharing_by_option.values():
        assert tier.count == 0
        assert tier.total_remaining == Decimal("0")


def test_build_dashboard_sharing_defaults_when_result_omitted():
    # Backward-compatible: no SharingResult means empty groups.
    credits = [_sharing_credit("c1")]
    lifecycle = LifecycleAnalyzer(SNAPSHOT_DATE).partition(credits)
    dashboard = build_dashboard(
        lifecycle, ExpirationAnalyzer(NOW).classify(credits), NewCreditResult()
    )
    for tier in dashboard.sharing_by_option.values():
        assert tier.count == 0


def test_build_dashboard_no_categories_all_unbound_are_shared_by_default():
    # No categories at all: credits without a full binding are all
    # "Shared by default" and the cost-category-based group is empty.
    credits = [_sharing_credit("c1"), _sharing_credit("c2")]
    dashboard = _sharing_dashboard(credits, [])
    assert dashboard.sharing_by_option[COST_CATEGORY_BASED].count == 0
    assert dashboard.sharing_by_option[SHARED_BY_DEFAULT].count == 2


# --------------------------------------------------------------------------
# Sharing table: all three groups populated together (task 19.4)
# Req 15.4
# --------------------------------------------------------------------------


def test_build_dashboard_sharing_two_way_mix_counts_and_totals():
    """Req 15.4: cost-category-based and "Shared by default" can each carry
    credits in the same dashboard, with per-group counts and totals matching
    the groups threaded through a SharingResult.
    """
    from aws_credits_reporter.sharing_classifier import SharingGroup, SharingResult

    cat = _sharing_credit("cat", remaining="250.00")
    plain = _sharing_credit("plain", remaining="5.00")
    credits = [cat, plain]
    groups = [
        SharingGroup(
            credit=cat,
            sharing_option=COST_CATEGORY_BASED,
            cost_category_name="Sharing Cat",
        ),
        SharingGroup(credit=plain, sharing_option=SHARED_BY_DEFAULT),
    ]
    sharing = SharingResult(groups=groups)
    lifecycle = LifecycleAnalyzer(SNAPSHOT_DATE).partition(credits)
    dashboard = build_dashboard(
        lifecycle, ExpirationAnalyzer(NOW).classify(credits), NewCreditResult(), sharing
    )

    assert dashboard.sharing_option_display(COST_CATEGORY_BASED) == (1, "$250.00")
    assert dashboard.sharing_option_display(SHARED_BY_DEFAULT) == (1, "$5.00")

def test_sharing_totals_reconcile_to_active_count_when_credit_ids_repeat():
    """Two records sharing a creditId must not be mixed up by id-keyed lookups."""
    from datetime import date, datetime, timezone
    from decimal import Decimal

    from aws_credits_reporter.dashboard import build_dashboard
    from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
    from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
    from aws_credits_reporter.models import Credit, CurrencyAmount, LifecycleStatus
    from aws_credits_reporter.new_credit_detector import NewCreditResult
    from aws_credits_reporter.sharing_classifier import SHARED_BY_DEFAULT, SharingClassifier

    def amount(value: str) -> CurrencyAmount:
        return CurrencyAmount(Decimal(value), "USD")

    active = Credit(creditId="DUP", endDate=date(2027, 1, 1), remainingAmount=amount("10"))
    expired = Credit(creditId="DUP", endDate=date(2026, 9, 1), remainingAmount=amount("7"))
    credits = [active, expired]
    snapshot = date(2026, 10, 22)

    lifecycle = LifecycleAnalyzer(snapshot).partition(credits)
    expiration = ExpirationAnalyzer(datetime(2026, 10, 22, tzinfo=timezone.utc)).classify(
        lifecycle.active
    )
    sharing = SharingClassifier().classify(credits, [])
    dashboard = build_dashboard(lifecycle, expiration, NewCreditResult(), sharing)

    assert dashboard.active_count == 1
    assert dashboard.sharing_by_option[SHARED_BY_DEFAULT].count == 1
    assert dashboard.sharing_by_option[SHARED_BY_DEFAULT].total_remaining == Decimal("10")
    assert dashboard.inactive_by_status[LifecycleStatus.EXPIRED].count == 1
