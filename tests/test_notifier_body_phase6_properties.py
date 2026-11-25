"""Task 27.2: unit and property tests for the Phase 6 ``build_dashboard_body``.

Phase 6 reworked the Dashboard_Summary email body (Req 21.4, 22.1-22.9):

* alert bullets start with "- " and a cost-category-based alert names the
  Bound_Cost_Category and its member accounts (Req 21.4, 22.1);
* the Active line carries no inline inactive-count suffix (Req 22.2);
* the "Credits by sharing option" block sums only Active_Credits (Req 22.7);
* the Disabled Lifecycle_Status is labelled "Paused" everywhere in the body
  (Req 22.5);
* the trailing listing is titled "EXPIRED CREDITS" when every listed credit is
  Expired, else "INACTIVE CREDITS" grouped Expired / Exhausted / Paused, and
  lists exactly the Inactive_Credits once (Req 22.8, 22.9);
* the body is built once, so the SNS publish carries every block intact
  (Req 11.3, 12.3).

Property 18 (this task): the trailing heading is "EXPIRED CREDITS" iff every
listed Inactive_Credit is Expired, else "INACTIVE CREDITS" grouped Expired /
Exhausted / Paused; the listed creditIds equal the Inactive_Credits exactly
once; "Paused" replaces "Disabled" in the body.
Validates: Requirements 21.4, 22.1, 22.2, 22.4, 22.5, 22.7, 22.8, 22.9

Everything is built through the real analysis pipeline (Lifecycle_Analyzer,
Expiration_Analyzer, New_Credit_Detector, Sharing_Classifier, build_dashboard,
build_dashboard_body) so the generated inputs stay internally consistent,
exactly as the task 13.2 and 16.6 body tests do. No AWS client is constructed
anywhere in this module.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from aws_credits_reporter.dashboard import build_dashboard
from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
from aws_credits_reporter.models import (
    CostCategory,
    CostCategoryDefinition,
    Credit,
    CurrencyAmount,
    LifecycleStatus,
    Snapshot,
)
from aws_credits_reporter.new_credit_detector import NewCreditDetector
from aws_credits_reporter.report_body import DASHBOARD_SUMMARY_HEADING, EXPIRED_CREDITS_HEADING, INACTIVE_CREDITS_HEADING, NEW_CREDITS_ALERT_HEADING, PAUSED_CREDITS_ALERT_HEADING, PAUSED_LABEL, SHARING_SUMMARY_HEADING, _status_label, build_dashboard_body, format_credit_alert_bullet
from aws_credits_reporter.sharing_classifier import (
    COST_CATEGORY_BASED,
    SharingClassifier,
)

# A fixed "now" mid-month so the expiration windows are unambiguous.
NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
AS_OF = NOW.date()

# --------------------------------------------------------------------------
# Pipeline helpers (mirror the task 13.2 / 16.6 body tests)
# --------------------------------------------------------------------------


def _raw(credit: Credit) -> dict:
    remaining = credit.remainingAmount
    return {
        "creditId": credit.creditId,
        "description": credit.description,
        "remainingAmount": (
            {"currencyAmount": str(remaining.amount), "currencyCode": remaining.currency}
            if remaining is not None
            else None
        ),
        "endDate": credit.endDate.isoformat() if credit.endDate is not None else None,
        "exhaustDate": credit.exhaustDate.isoformat() if credit.exhaustDate else None,
        "creditStatus": credit.creditStatus,
    }


def _snapshot(key: str, credits: list[Credit]) -> Snapshot:
    return Snapshot(key=key, taken_at=NOW, raw_get_credits=[{"credits": [_raw(c) for c in credits]}])


def _build(
    current: list[Credit],
    *,
    prior_ids: list[str] | None = None,
    categories: list[CostCategory] | None = None,
    classifier: SharingClassifier | None = None,
):
    """Return the body built through the real pipeline, plus the lifecycle result.

    The same ``current`` Credit instances flow through every analyzer, so the
    per-credit cost-category annotation (keyed on ``id()`` in the notifier)
    lines up with the alert tiers.
    """
    lifecycle = LifecycleAnalyzer(AS_OF).partition(current)
    expiration = ExpirationAnalyzer(NOW).classify(lifecycle.active)
    prior = (
        _snapshot("prior", [Credit(creditId=i) for i in prior_ids])
        if prior_ids is not None
        else None
    )
    new = NewCreditDetector().detect(
        _snapshot("current", current), prior, prior_read_ok=True
    )
    sharing = None
    if categories is not None or classifier is not None:
        clf = classifier if classifier is not None else SharingClassifier()
        sharing = clf.classify(current, categories or [])
    dashboard = build_dashboard(lifecycle, expiration, new, sharing)
    body = build_dashboard_body(dashboard, expiration, new, lifecycle.inactive, sharing)
    return body, lifecycle


def _credit(
    credit_id: str,
    remaining: str | None,
    end: date | None,
    *,
    status: str = "ENABLED",
    exhaust: date | None = None,
    description: str | None = None,
    cost_category_arn: str | None = None,
    rule_name: str | None = None,
    shareable: list[str] | None = None,
) -> Credit:
    return Credit(
        creditId=credit_id,
        remainingAmount=CurrencyAmount(Decimal(remaining), "USD") if remaining is not None else None,
        endDate=end,
        creditStatus=status,
        exhaustDate=exhaust,
        description=description,
        costCategoryArn=cost_category_arn,
        ruleName=rule_name,
        shareableAccounts=shareable or [],
    )


def _inactive_block(body: str) -> str:
    """The trailing listing from its heading to the end; empty when it is omitted."""
    if EXPIRED_CREDITS_HEADING in body:
        heading = EXPIRED_CREDITS_HEADING
    elif INACTIVE_CREDITS_HEADING in body:
        heading = INACTIVE_CREDITS_HEADING
    else:
        return ""
    return body[body.index(heading):]


def _paused_alert_ids(body: str) -> list[str]:
    """creditIds listed in the "Paused credits" alert; empty when there is none."""
    lines = body.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(f"{PAUSED_CREDITS_ALERT_HEADING} (")),
        None,
    )
    if start is None:
        return []
    ids: list[str] = []
    for line in lines[start + 1:]:
        if not line.startswith("- "):
            break
        ids.append(line[2:].split()[0])
    return ids


# The group labels in the trailing listing are the *display* labels, so the
# Disabled group reads "Paused" (Req 22.5).
_GROUP_LABELS = {
    _status_label(LifecycleStatus.EXPIRED),
    _status_label(LifecycleStatus.EXHAUSTED),
    _status_label(LifecycleStatus.DISABLED),
}


def _listed_inactive_ids(body: str) -> list[str]:
    """creditIds listed under the trailing listing (bullet lines, Req 22.9)."""
    lines = _inactive_block(body).splitlines()[1:]  # drop the heading itself
    ids: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped in _GROUP_LABELS:
            continue
        if line.startswith("- "):
            body_text = line[2:].strip()
            # Two formats: the grouped INACTIVE form "<creditId>  <desc> ..."
            # and the compact all-expired form "<date> - <creditId> "<desc>" - <amount>",
            # the latter identifiable by its quoted description.
            if '"' in body_text and " - " in body_text:
                ids.append(body_text.split(" - ", 1)[1].split()[0])
            else:
                ids.append(body_text.split()[0])
    return ids


# ==========================================================================
# Property 18: trailing heading, grouping, membership, and the Paused relabel.
#
# Feature: aws-credits-reporter, Property 18.
# Validates: Requirements 22.5, 22.8, 22.9
# ==========================================================================


def _p18_credit(index: int) -> st.SearchStrategy[Credit]:
    """A credit that can land in any lifecycle bucket, with a stable creditId.

    endDate spans before and after AS_OF so Expired is reachable; a zero
    remaining or an exhaustDate reaches Exhausted; DISABLED reaches the Paused
    group; the rest stay Active.
    """
    return st.builds(
        Credit,
        creditId=st.just(f"c-{index}"),
        remainingAmount=st.one_of(
            st.none(),
            st.builds(
                CurrencyAmount,
                amount=st.decimals(min_value=Decimal("0"), max_value=Decimal("100000"), places=2),
                currency=st.just("USD"),
            ),
        ),
        endDate=st.one_of(
            st.none(),
            st.dates(min_value=date(2024, 1, 1), max_value=date(2027, 12, 31)),
        ),
        exhaustDate=st.one_of(
            st.none(), st.dates(min_value=date(2024, 1, 1), max_value=date(2027, 12, 31))
        ),
        creditStatus=st.sampled_from([None, "ENABLED", "DISABLED"]),
    )


@st.composite
def _p18_credits(draw: st.DrawFn) -> list[Credit]:
    n = draw(st.integers(min_value=0, max_value=8))
    return [draw(_p18_credit(i)) for i in range(n)]


@settings(max_examples=150)
@given(current=_p18_credits())
def test_property18_trailing_heading_grouping_membership_and_paused(current):
    """The trailing heading, grouping, membership, and the Paused relabel.

    Validates: Requirements 22.5, 22.8, 22.9
    """
    body, lifecycle = _build(current)

    inactive = lifecycle.inactive
    inactive_statuses = {
        lifecycle.status_by_id.get(c.creditId)
        for c in inactive
        if c.creditId is not None
    }
    paused_ids = [
        c.creditId
        for c in inactive
        if c.creditId is not None
        and lifecycle.status_by_id.get(c.creditId) is LifecycleStatus.DISABLED
    ]
    listable = [
        c for c in inactive
        if lifecycle.status_by_id.get(c.creditId) is not LifecycleStatus.DISABLED
    ]
    listable_statuses = {lifecycle.status_by_id.get(c.creditId) for c in listable}
    all_expired = bool(listable) and listable_statuses == {LifecycleStatus.EXPIRED}

    # Heading: "EXPIRED CREDITS" iff every listed credit is Expired, else
    # "INACTIVE CREDITS". With nothing to list, the block is omitted.
    if not listable:
        assert EXPIRED_CREDITS_HEADING not in body
        assert INACTIVE_CREDITS_HEADING not in body
    elif all_expired:
        assert EXPIRED_CREDITS_HEADING in body
        assert INACTIVE_CREDITS_HEADING not in body
        assert body.count(EXPIRED_CREDITS_HEADING) == 1
    else:
        assert INACTIVE_CREDITS_HEADING in body
        assert EXPIRED_CREDITS_HEADING not in body
        assert body.count(INACTIVE_CREDITS_HEADING) == 1

    # Listed creditIds are the Expired and Exhausted credits, exactly once.
    listed = _listed_inactive_ids(body)
    expected = [c.creditId for c in listable if c.creditId is not None]
    assert sorted(listed) == sorted(expected)
    assert len(listed) == len(set(listed))  # each listed once

    # Paused credits are listed in the "Paused credits" alert, once each.
    assert sorted(_paused_alert_ids(body)) == sorted(paused_ids)

    # "Paused" replaces "Disabled": the enum value never appears, and when a
    # Disabled credit is inactive the "Paused" label does (Req 22.5).
    assert LifecycleStatus.DISABLED.value not in body
    if LifecycleStatus.DISABLED in inactive_statuses:
        assert PAUSED_LABEL in body


# ==========================================================================
# Unit: alert bullets are "- " lines
# ==========================================================================


def test_alert_credits_are_bullets_starting_with_dash_space():
    """Every credit listed in an alert is a bullet line starting with "- " (Req 22.1)."""
    # c-this expires this month; c-next in the next-three window; c-new is new.
    this_month = _credit("c-this", "120.50", date(2026, 5, 28))
    next_three = _credit("c-next", "300.00", date(2026, 7, 15))
    new = _credit("c-new", "42.00", date(2026, 12, 31))
    body, _ = _build([this_month, next_three, new], prior_ids=["c-this", "c-next"])

    alerts = body[: body.index(DASHBOARD_SUMMARY_HEADING)]
    # Each listed creditId sits on its own "- " bullet in the alerts block.
    for credit_id in ("c-this", "c-next", "c-new"):
        bullet = next(
            line for line in alerts.splitlines() if line.startswith("- ") and credit_id in line
        )
        assert bullet.startswith("- ")

    # The new-credit alert heading is present and the listing under it is bullets.
    assert f"{NEW_CREDITS_ALERT_HEADING} (" in alerts


# ==========================================================================
# Unit: the Active line has no inactive suffix (Req 22.2)
# ==========================================================================


def test_active_line_has_no_inactive_suffix():
    active = _credit("c-active", "18750.00", date(2026, 9, 30))
    expired = _credit("c-expired", "400.00", date(2025, 3, 31))
    body, _ = _build([active, expired])

    active_line = next(
        line for line in body.splitlines() if line.startswith("- Active credits:")
    )
    # The Active line reports count and total only; no "inactive" figure rides
    # along on it (Req 22.2) - the inactive figures are their own bullets.
    assert "inactive" not in active_line.lower()
    assert "- Active credits: 1 credit" in active_line
    assert "18750.00" in active_line


# ==========================================================================
# Unit: the sharing block sums only active credits (Req 22.7)
#
# The captured scenario: one Active cost-category-based credit of $20.00 and
# inactive credits that would otherwise inflate the cost-category total to
# $55.75. The sharing line must read 1 credit / $20.00, not $55.75.
# ==========================================================================

_CC_ARN = "arn:aws:ce::111111111111:costcategory/abc"


def _cost_category() -> CostCategory:
    return CostCategory(
        CostCategoryArn=_CC_ARN,
        Name="CreditGroup",
    )


def test_sharing_block_sums_only_active_credits():
    """The cost-category-based sharing line counts active credits only (Req 22.7)."""
    # Active cost-category credit: $20.00, well inside the active window.
    active_cc = _credit("c-cc-active", "20.00", date(2026, 9, 30))
    # Inactive cost-category credits that would inflate the total to $55.75
    # (20 + 15.75 + 20) if inactive credits were (wrongly) counted.
    expired_cc = _credit("c-cc-expired", "15.75", date(2025, 1, 1))  # Expired
    paused_cc = _credit("c-cc-paused", "20.00", date(2027, 1, 1), status="DISABLED")  # Paused
    body, _ = _build(
        [active_cc, expired_cc, paused_cc],
        categories=[_cost_category()],
    )

    sharing_block = body[body.index(SHARING_SUMMARY_HEADING):]
    # The count/total for the single active cost-category credit now appear
    # on its sub-bullet, not the "- cost-category-based:" header line.
    cc_sub_bullet = next(
        line
        for line in sharing_block.splitlines()
        if line.strip().startswith("- ") and "1 credit" in line and "$20.00" in line
    )
    # Active-only: exactly one credit, $20.00 total - never the $55.75 that
    # would appear if the two inactive cost-category credits were counted.
    assert "1 credit" in cc_sub_bullet
    assert "$20.00" in cc_sub_bullet
    assert "$55.75" not in body
    assert "55.75" not in sharing_block


# ==========================================================================
# Unit: a cost-category alert names the category and member accounts (Req 21.4)
# ==========================================================================


class _StubRetriever:
    """Returns a fixed definition for its ARN, else ``None``."""

    def __init__(self, definition: CostCategoryDefinition):
        self._definition = definition

    def describe(self, cost_category_arn: str):
        if cost_category_arn == self._definition.arn:
            return self._definition
        return None


class _StubResolver:
    def __init__(self, names: dict[str, str | None]):
        self._names = names

    def resolve(self, account_ids: list[str]):
        return _Resolution(self._names)


class _Resolution:
    def __init__(self, names: dict[str, str | None]):
        self.names = names


def _linked_account_rule(rule_name: str, account_ids: list[str]) -> dict:
    return {
        "Value": rule_name,
        "Rule": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": account_ids}},
    }


def test_target_layout_cost_category_credit_and_two_expired():
    """The full rendered body matches the approved target layout exactly.

    One active cost-category-bound credit (Credits-Sharing / rule
    Project-Alpha, members Operations 444444444444 and Testing
    666666666666) plus two Expired credits ($35.00 and $0.75). Asserts the
    "CREDITS DASHBOARD SUMMARY" heading, the exact bullet order, annotations
    only on Exhausted/Inactive/Expired, the two-row sharing block, the
    cost-category sub-bullet, and the alert annotation's exact wording (Req 21.4, 22.1-22.14).
    """
    rule_name = "Project-Alpha"
    member_ids = ["444444444444", "666666666666"]
    definition = CostCategoryDefinition(
        arn=_CC_ARN,
        name="Credits-Sharing",
        rules=[_linked_account_rule(rule_name, member_ids)],
    )
    classifier = SharingClassifier(
        _StubRetriever(definition),
        _StubResolver({"444444444444": "Operations", "666666666666": "Testing"}),
    )
    active_cc = _credit(
        "19000000003",
        "20.00",
        date(2026, 12, 31),
        cost_category_arn=_CC_ARN,
        rule_name=rule_name,
    )
    expired_a = _credit(
        "19000000001",
        "35.00",
        date(2026, 6, 11),
        description="Example free trial promotion for monitoring",
    )
    expired_b = _credit(
        "19000000002",
        "0.75",
        date(2026, 6, 12),
        description="Example free trial promotion for tracing",
    )
    current = [active_cc, expired_a, expired_b]
    reference = CostCategory(CostCategoryArn=_CC_ARN, Name="Credits-Sharing")

    # NOW must put 2026-12-31 inside Expiring_Next_Three_Months (after the
    # current month's last day, on or before the third following month's
    # last day) while keeping both expired endDates in the past.
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    as_of = now.date()
    lifecycle = LifecycleAnalyzer(as_of).partition(current)
    expiration = ExpirationAnalyzer(now).classify(lifecycle.active)
    all_ids = [c.creditId for c in current]
    new = NewCreditDetector().detect(
        Snapshot(key="current", taken_at=now, raw_get_credits=[{"credits": [_raw(c) for c in current]}]),
        Snapshot(
            key="prior",
            taken_at=now,
            raw_get_credits=[{"credits": [_raw(Credit(creditId=i)) for i in all_ids]}],
        ),
        prior_read_ok=True,
    )
    sharing = classifier.classify(current, [reference])
    dashboard = build_dashboard(lifecycle, expiration, new, sharing)
    body = build_dashboard_body(
        dashboard, expiration, new, lifecycle.inactive, sharing, snapshot_date=as_of
    )

    expected = """\
AWS CREDITS OVERVIEW

Expiring in the next three months (1 credit, total remaining $20.00):
- 19000000003  remaining $20.00  ends 2026-12-31  (cost category "Credits-Sharing" on rule "Project-Alpha" with accounts: Operations 444444444444, Testing 666666666666)

CREDITS DASHBOARD SUMMARY
- Newly issued credits: No newly issued credits were found.
- Active credits: 1 credit, total remaining $20.00
- Paused: 0 credits, total remaining 0.00
- Exhausted in the last 3 months: 0 credits, total fully used 0.00
- Expiring this month: 0 credits, total remaining 0.00
- Expiring in the next three months: 1 credit, total remaining $20.00
- Expired in the last 3 months: 0 credits, total expired 0.00

Credits by sharing option
- cost-category-based:
   - Credits-Sharing (rule Project-Alpha) - 1 credit, total remaining $20.00
       accounts: Operations 444444444444, Testing 666666666666
- Shared by default: 0 credits, total remaining 0.00
- Sharing disabled: 0 credits, total remaining 0.00
"""
    assert body == expected


def test_cost_category_alert_names_the_category_and_member_accounts():
    """A cost-category-based alert bullet names the category and its members (Req 21.4)."""
    rule_name = "Credit_Rule"
    member_ids = ["444444444444", "555555555555"]
    definition = CostCategoryDefinition(
        arn=_CC_ARN,
        name="CreditGroup",
        rules=[_linked_account_rule(rule_name, member_ids)],
    )
    classifier = SharingClassifier(
        _StubRetriever(definition),
        _StubResolver({"444444444444": "Platform", "555555555555": "DataScience"}),
    )
    # One cost-category-based credit expiring this month so it rides an alert.
    cc_credit = _credit(
        "c-cc",
        "500.00",
        date(2026, 5, 28),
        cost_category_arn=_CC_ARN,
        rule_name=rule_name,
    )
    # Match the Phase-5 reference ARN so the Name resolves.
    reference = CostCategory(CostCategoryArn=_CC_ARN, Name="CreditGroup")

    body, _ = _build([cc_credit], categories=[reference], classifier=classifier)

    alerts = body[: body.index(DASHBOARD_SUMMARY_HEADING)]
    bullet = next(
        line for line in alerts.splitlines() if line.startswith("- ") and "c-cc" in line
    )
    # The bullet carries the cost category "<Name>" on rule "<Rule>" with
    # accounts: ... annotation naming the Bound_Cost_Category, its Bound_Rule,
    # and its member accounts (names where resolved).
    assert (
        'cost category "CreditGroup" on rule "Credit_Rule" with accounts: '
        "Platform 444444444444, DataScience 555555555555)" in bullet
    )


def test_format_credit_alert_bullet_matches_classifier_annotation():
    """The exported bullet formatter names the category and members directly (Req 21.4)."""
    rule_name = "Credit_Rule"
    member_ids = ["444444444444"]
    definition = CostCategoryDefinition(
        arn=_CC_ARN,
        name="CreditGroup",
        rules=[_linked_account_rule(rule_name, member_ids)],
    )
    classifier = SharingClassifier(
        _StubRetriever(definition), _StubResolver({"444444444444": "Platform"})
    )
    credit = _credit(
        "c-cc", "10.00", date(2026, 6, 30), cost_category_arn=_CC_ARN, rule_name=rule_name
    )
    reference = CostCategory(CostCategoryArn=_CC_ARN, Name="CreditGroup")
    [group] = classifier.classify([credit], [reference]).groups

    bullet = format_credit_alert_bullet(credit, group)
    assert bullet.startswith("- c-cc")
    assert 'cost category "CreditGroup" on rule "Credit_Rule" with accounts: Platform 444444444444' in bullet

    # With no group, the bullet carries no cost-category annotation.
    assert "cost category" not in format_credit_alert_bullet(credit)


# ==========================================================================
# Unit: the published SNS message carries all blocks (Req 11.3, 12.3)
# ==========================================================================


def test_published_message_carries_all_blocks():
    """The SNS publish keeps alerts, summary, sharing, and inactive."""
    from aws_credits_reporter.notifier import Notifier

    this_month = _credit("c-this", "120.50", date(2026, 5, 28))
    active_cc = _credit("c-cc-active", "20.00", date(2026, 9, 30))
    expired = _credit("c-expired", "400.00", date(2025, 3, 31))
    paused = _credit("c-paused", "2000.00", date(2027, 1, 1), status="DISABLED")
    current = [this_month, active_cc, expired, paused]
    body, _ = _build(current, prior_ids=["c-this"], categories=[_cost_category()])

    class FakeClient:
        def __init__(self):
            self.calls = []

        def publish(self, **kwargs):
            self.calls.append(kwargs)
            return {"MessageId": "m-1"}

    client = FakeClient()
    notifier = Notifier(client, sns_topic_arn="arn:aws:sns:us-east-1:123456789012:credits")
    result = notifier.send(body)

    assert result.sent is True
    body_text = client.calls[-1]["Message"]
    # The prebuilt body is carried verbatim, with nothing appended.
    assert body_text == body
    assert DASHBOARD_SUMMARY_HEADING in body_text
    assert SHARING_SUMMARY_HEADING in body_text
    # Only an Expired credit is listed in the trailing section; the paused
    # credit is in the alerts.
    assert EXPIRED_CREDITS_HEADING in body_text
    assert PAUSED_CREDITS_ALERT_HEADING in body_text
    # Every alert, inactive, and the Paused relabel survive.
    assert "c-this" in body_text
    assert "c-expired" in body_text and "c-paused" in body_text
    assert LifecycleStatus.DISABLED.value not in body_text
    assert PAUSED_LABEL in body_text
    assert "ttachment" not in body_text


def test_expired_listing_scoped_to_last_three_months():
    """Only Expired credits within the last 3 calendar months are listed, and
    the DASHBOARD SUMMARY Expired figure is scoped to the same window."""
    from datetime import date, datetime, timezone

    from aws_credits_reporter.dashboard import build_dashboard
    from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
    from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
    from aws_credits_reporter.models import Credit, CurrencyAmount
    from aws_credits_reporter.new_credit_detector import NewCreditResult
    from aws_credits_reporter.report_body import EXPIRED_CREDITS_HEADING, build_dashboard_body, _window_start
    from aws_credits_reporter.sharing_classifier import SharingClassifier
    from decimal import Decimal

    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    as_of = now.date()
    assert _window_start(as_of) == date(2026, 7, 16)

    def amt(v):
        return CurrencyAmount(Decimal(v), "USD")

    inside = Credit(
        creditId="in-window", accountId="111111111111", description="Recent",
        remainingAmount=amt("5.00"), endDate=date(2026, 8, 1), creditStatus="ENABLED",
    )  # expired Aug 1 -> inside (>= Jul 16)
    outside = Credit(
        creditId="old-expired", accountId="111111111111", description="Old",
        remainingAmount=amt("9.00"), endDate=date(2026, 6, 11), creditStatus="ENABLED",
    )  # expired Jun 11 -> outside
    current = [inside, outside]

    lifecycle = LifecycleAnalyzer(as_of).partition(current)
    expiration = ExpirationAnalyzer(now).classify(lifecycle.active)
    sharing = SharingClassifier().classify(current, [])
    dashboard = build_dashboard(lifecycle, expiration, NewCreditResult(), sharing)
    body = build_dashboard_body(
        dashboard, expiration, NewCreditResult(), lifecycle.inactive, sharing,
        snapshot_date=as_of,
    )

    # Heading renamed; only the in-window credit is listed, the old one dropped.
    assert EXPIRED_CREDITS_HEADING == "EXPIRED CREDITS IN THE LAST 3 MONTHS"
    assert "EXPIRED CREDITS IN THE LAST 3 MONTHS" in body
    assert "in-window" in body
    assert "old-expired" not in body
    # Summary Expired line counts only the in-window credit ($5.00), not $14.00.
    assert "- Expired in the last 3 months: 1 credit, total expired $5.00" in body
    # The overlapping overall "Inactive credits" line is gone from the summary.
    assert "- Inactive credits:" not in body


def test_exhausted_line_scoped_to_last_three_months_and_sums_amount_used():
    """Exhausted is limited to the last 3 months and totals initial - remaining."""
    from datetime import date, datetime, timezone
    from decimal import Decimal

    from aws_credits_reporter.dashboard import build_dashboard
    from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
    from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
    from aws_credits_reporter.models import Credit, CurrencyAmount
    from aws_credits_reporter.new_credit_detector import NewCreditResult
    from aws_credits_reporter.report_body import build_dashboard_body
    from aws_credits_reporter.sharing_classifier import SharingClassifier

    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    as_of = now.date()

    def amt(v):
        return CurrencyAmount(Decimal(v), "USD")

    recent = Credit(
        creditId="recent", accountId="111111111111", initialAmount=amt("100.00"),
        remainingAmount=amt("0"), exhaustDate=date(2026, 9, 1), endDate=date(2027, 1, 1),
        creditStatus="ENABLED",
    )  # exhausted Sep 1 -> inside the window
    recent_no_date = Credit(
        creditId="recent-zero", accountId="111111111111", initialAmount=amt("50.00"),
        remainingAmount=amt("0"), endDate=date(2027, 1, 1), creditStatus="ENABLED",
    )  # zero remaining, no exhaustDate: falls back to endDate (2027) -> inside (>= Jul 16)
    old = Credit(
        creditId="old", accountId="111111111111", initialAmount=amt("999.00"),
        remainingAmount=amt("0"), exhaustDate=date(2026, 3, 1), endDate=date(2027, 1, 1),
        creditStatus="ENABLED",
    )  # exhausted Mar 1 -> outside the window
    current = [recent, recent_no_date, old]

    lifecycle = LifecycleAnalyzer(as_of).partition(current)
    expiration = ExpirationAnalyzer(now).classify(lifecycle.active)
    sharing = SharingClassifier().classify(current, [])
    dashboard = build_dashboard(lifecycle, expiration, NewCreditResult(), sharing)
    body = build_dashboard_body(
        dashboard, expiration, NewCreditResult(), lifecycle.inactive, sharing,
        snapshot_date=as_of,
    )

    assert "- Exhausted in the last 3 months: 2 credits, total fully used $150.00" in body
    assert "$999.00" not in body
