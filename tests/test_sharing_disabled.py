"""Sharing disabled and member sharing state in the Sharing_Classifier and the dashboard."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from aws_credits_reporter.dashboard import build_dashboard
from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
from aws_credits_reporter.models import (
    CostCategory,
    CostCategoryDefinition,
    Credit,
    CurrencyAmount,
    Snapshot,
)
from aws_credits_reporter.new_credit_detector import NewCreditDetector
from aws_credits_reporter.sharing_classifier import (
    COST_CATEGORY_BASED,
    SHARED_BY_DEFAULT,
    SHARING_DISABLED,
    SharingClassifier,
)

ARN = "arn:aws:ce::111111111111:costcategory/abc"
RULE = {
    "Value": "FirstOpen",
    "Rule": {
        "Dimensions": {
            "Key": "LINKED_ACCOUNT",
            "Values": ["555555555555", "666666666666"],
        }
    },
}
CATEGORY = CostCategory(CostCategoryArn=ARN, Name="OpenSharing")


class StubRetriever:
    def describe(self, arn):
        return CostCategoryDefinition(arn=arn, name="OpenSharing", rules=[RULE], processing_complete=False)


class StubResolver:
    def __init__(self, names):
        self._names = names

    def resolve(self, ids):
        class Resolution:
            pass

        resolution = Resolution()
        resolution.names = {i: self._names.get(i) for i in ids}
        return resolution


class StubPreferences:
    def __init__(self, enabled, names=None):
        self._enabled = enabled
        self._names = names or {}

    def is_enabled(self, account_id):
        return self._enabled.get(account_id)

    def account_name(self, account_id):
        return self._names.get(account_id)


def _credit(credit_id, *, sharing_type="DEFAULT", account="666666666666", arn=None, rule=None, shareable=None):
    return Credit(
        creditId=credit_id,
        accountId=account,
        creditSharingType=sharing_type,
        costCategoryArn=arn,
        ruleName=rule,
        shareableAccounts=shareable or [],
        remainingAmount=CurrencyAmount(Decimal("20"), "USD"),
        endDate=date(2027, 1, 1),
        creditStatus="ENABLED",
    )


def _classify(credits, *, prefs=None, resolver=None):
    classifier = SharingClassifier(StubRetriever(), resolver, prefs)
    return classifier.classify(credits, [CATEGORY]).groups


def test_disabled_credit_without_binding_is_sharing_disabled_with_owner_name():
    [group] = _classify(
        [_credit("c1", sharing_type="DISABLED", account="333333333333")],
        resolver=StubResolver({"333333333333": "Audit"}),
    )

    assert group.sharing_option == SHARING_DISABLED
    assert group.owner_account_name == "Audit"
    assert group.cost_category_arn is None


def test_owner_name_falls_back_to_the_preferences_name():
    [group] = _classify(
        [_credit("c1", sharing_type="DISABLED", account="333333333333")],
        prefs=StubPreferences({}, {"333333333333": "Audit"}),
        resolver=StubResolver({}),
    )

    assert group.owner_account_name == "Audit"


def test_disabled_credit_with_a_binding_keeps_the_binding_details():
    [group] = _classify(
        [_credit("c1", sharing_type="DISABLED", arn=ARN, rule="FirstOpen")],
        prefs=StubPreferences({"555555555555": True, "666666666666": False}),
    )

    assert group.sharing_option == SHARING_DISABLED
    assert group.cost_category_name == "OpenSharing"
    assert group.rule_name == "FirstOpen"
    assert [(m.account_id, m.sharing_enabled) for m in group.member_accounts] == [
        ("555555555555", True),
        ("666666666666", False),
    ]


def test_disabled_wins_over_shareable_accounts():
    [group] = _classify([_credit("c1", sharing_type="DISABLED", shareable=["444455556666"])])

    assert group.sharing_option == SHARING_DISABLED


def test_default_credits_are_unchanged_by_the_new_option():
    groups = _classify(
        [
            _credit("bound", arn=ARN, rule="FirstOpen"),
            _credit("plain"),
            _credit("shareable", shareable=["444455556666"]),
            _credit("partial", arn=ARN),
        ]
    )

    assert [g.sharing_option for g in groups] == [
        COST_CATEGORY_BASED,
        SHARED_BY_DEFAULT,
        SHARED_BY_DEFAULT,
        SHARED_BY_DEFAULT,
    ]


def test_members_carry_their_sharing_state_and_unknown_stays_none():
    [group] = _classify(
        [_credit("bound", arn=ARN, rule="FirstOpen")],
        prefs=StubPreferences({"555555555555": True}),
    )

    assert [(m.account_id, m.sharing_enabled) for m in group.member_accounts] == [
        ("555555555555", True),
        ("666666666666", None),  # not listed: unknown, not "off"
    ]


def test_without_preferences_members_have_no_sharing_state():
    [group] = _classify([_credit("bound", arn=ARN, rule="FirstOpen")])

    assert all(m.sharing_enabled is None for m in group.member_accounts)


def test_dashboard_counts_sharing_disabled_over_active_credits_only():
    credits = [
        _credit("off", sharing_type="DISABLED"),
        _credit("on"),
    ]
    paused_and_off = Credit(
        creditId="paused",
        creditSharingType="DISABLED",
        creditStatus="DISABLED",
        remainingAmount=CurrencyAmount(Decimal("5"), "USD"),
        endDate=date(2027, 1, 1),
    )
    all_credits = credits + [paused_and_off]
    sharing = SharingClassifier().classify(all_credits, [])
    lifecycle = LifecycleAnalyzer(date(2026, 10, 26)).partition(all_credits)
    expiration = ExpirationAnalyzer(
        __import__("datetime").datetime(2026, 10, 26, tzinfo=__import__("datetime").timezone.utc)
    ).classify(lifecycle.active)
    new = NewCreditDetector().detect(
        Snapshot(key="c", taken_at=__import__("datetime").datetime(2026, 10, 26, tzinfo=__import__("datetime").timezone.utc), raw_get_credits=[]),
        None,
    )

    dashboard = build_dashboard(lifecycle, expiration, new, sharing)

    # The paused credit is inactive, so it is not counted in the sharing figures.
    assert dashboard.sharing_by_option[SHARING_DISABLED].count == 1
    assert dashboard.sharing_by_option[SHARED_BY_DEFAULT].count == 1
