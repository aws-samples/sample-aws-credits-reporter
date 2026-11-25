"""The overview title, the Paused credits alert, and the sharing details in the body.

Covers the layout with: the ``AWS CREDITS OVERVIEW FOR PAYER <id> (<name>)``
title, the "Paused credits" alert, ``(sharing off)`` marks on cost category
members, the "Sharing disabled" group, and the omitted trailing listing. Also
covers how the orchestrator fills in the payer name and reports unavailable
credit sharing preferences.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from aws_credits_reporter.credit_retriever import RetrievalOutcome
from aws_credits_reporter.dashboard import build_dashboard
from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
from aws_credits_reporter.models import (
    CostCategory,
    CostCategoryDefinition,
    Credit,
    CurrencyAmount,
    Snapshot,
    parse_credits,
)
from aws_credits_reporter.new_credit_detector import NewCreditDetector
from aws_credits_reporter.notifier import NotifyResult
from aws_credits_reporter.orchestrator import (
    SHARING_PREFERENCES_UNAVAILABLE_PREFIX,
    Orchestrator,
)
from aws_credits_reporter.report_body import (
    OVERVIEW_HEADING,
    build_dashboard_body,
    overview_heading,
)
from aws_credits_reporter.sharing_classifier import SharingClassifier
from aws_credits_reporter.sharing_preferences import CreditSharingPreferences

PAYER = "123456789012"
PAYER_NAME = "Control Tower Management Account"
NOW = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)

ARN_ALPHA = "arn:aws:ce::123456789012:costcategory/51d1c892"
ARN_OPEN = "arn:aws:ce::123456789012:costcategory/7d605c4a"

ACCOUNT_NAMES = {
    "222222222222": "Operations",
    "444455556666": "Testing",
    "555555555555": "Backup Account",
    "666666666666": "Log Archive",
    "333333333333": "Audit",
}
SHARING_OFF = {"666666666666", "333333333333"}


def _rule(name: str, accounts: list[str]) -> dict:
    return {
        "Value": name,
        "Rule": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": accounts}},
    }


DEFINITIONS = {
    ARN_ALPHA: CostCategoryDefinition(
        arn=ARN_ALPHA,
        name="Credits-Sharing",
        rules=[_rule("Project-Alpha", ["222222222222", "444455556666"])],
    ),
    ARN_OPEN: CostCategoryDefinition(
        arn=ARN_OPEN,
        name="OpenSharing",
        rules=[_rule("FirstOpen", ["555555555555", "666666666666"])],
        processing_complete=False,
    ),
}
CATEGORIES = [
    CostCategory(CostCategoryArn=ARN_ALPHA, Name="Credits-Sharing"),
    CostCategory(CostCategoryArn=ARN_OPEN, Name="OpenSharing"),
]


class StubRetriever:
    def describe(self, arn):
        return DEFINITIONS.get(arn)


class StubResolver:
    consulted = True
    unavailable = False

    def __init__(self, names=None):
        self._names = ACCOUNT_NAMES if names is None else names

    def resolve(self, ids):
        class Resolution:
            pass

        resolution = Resolution()
        resolution.names = {i: self._names.get(i) for i in ids}
        return resolution


class StubPreferences:
    consulted = True
    unavailable = False

    def is_enabled(self, account_id):
        if account_id in ACCOUNT_NAMES:
            return account_id not in SHARING_OFF
        return None

    def account_name(self, account_id):
        return ACCOUNT_NAMES.get(account_id)


def _credit(
    credit_id: str,
    account: str,
    description: str,
    end: date,
    *,
    sharing_type: str = "DEFAULT",
    status: str = "ENABLED",
    arn: str | None = None,
    rule: str | None = None,
) -> Credit:
    return Credit(
        creditId=credit_id,
        accountId=account,
        description=description,
        remainingAmount=CurrencyAmount(Decimal("20"), "USD"),
        endDate=end,
        creditStatus=status,
        creditSharingType=sharing_type,
        costCategoryArn=arn,
        ruleName=rule,
    )


def _raw(credit: Credit) -> dict:
    return {
        "creditId": credit.creditId,
        "accountId": credit.accountId,
        "description": credit.description,
        "remainingAmount": {"currencyAmount": "20", "currencyCode": "USD"},
        "endDate": credit.endDate.isoformat(),
        "creditStatus": credit.creditStatus,
        "creditSharingType": credit.creditSharingType,
        "costCategoryArn": credit.costCategoryArn,
        "ruleName": credit.ruleName,
    }


# The account's state on 26 Oct 2026: two new credits (19000000006 on Audit,
# 19000000007 on Log Archive and paused), one active cost category credit, and
# two June expirations that are older than the 3-month window.
AUDIT_CREDIT = _credit(
    "19000000006", "333333333333", "DevOps Agent Enterprise Support Credit",
    date(2027, 3, 31), sharing_type="DISABLED",
)
PAUSED_CREDIT = _credit(
    "19000000007", "666666666666", "Example Promotion for Security Customers",
    date(2026, 10, 31), sharing_type="DISABLED", status="DISABLED", arn=ARN_OPEN, rule="FirstOpen",
)
ALPHA_CREDIT = _credit(
    "19000000003", "222222222222", "Example marketplace test credit",
    date(2026, 12, 31), arn=ARN_ALPHA, rule="Project-Alpha",
)
EXPIRED_A = _credit("19000000001", "222222222222", "Old one", date(2026, 6, 11))
EXPIRED_B = _credit("19000000002", "222222222222", "Old two", date(2026, 6, 12))
CREDITS = [PAUSED_CREDIT, AUDIT_CREDIT, EXPIRED_A, EXPIRED_B, ALPHA_CREDIT]


def _body(credits: list[Credit], *, preferences=None, name=PAYER_NAME) -> str:
    as_of = NOW.date()
    lifecycle = LifecycleAnalyzer(as_of).partition(credits)
    expiration = ExpirationAnalyzer(NOW).classify(lifecycle.active)
    prior_ids = {"19000000003", "19000000001", "19000000002"}
    prior = Snapshot(
        key="prior",
        taken_at=NOW,
        raw_get_credits=[{"credits": [_raw(c) for c in credits if c.creditId in prior_ids]}],
    )
    current = Snapshot(key="current", taken_at=NOW, raw_get_credits=[{"credits": [_raw(c) for c in credits]}])
    new = NewCreditDetector().detect(current, prior, prior_read_ok=True)
    sharing = SharingClassifier(StubRetriever(), StubResolver(), preferences).classify(credits, CATEGORIES)
    dashboard = build_dashboard(lifecycle, expiration, new, sharing)
    return build_dashboard_body(
        dashboard, expiration, new, lifecycle.inactive, sharing,
        snapshot_date=as_of, payer_account_id=PAYER, payer_account_name=name,
    )


EXPECTED_LAYOUT = f"""\
AWS CREDITS OVERVIEW FOR PAYER {PAYER} ({PAYER_NAME})

New credits (2 credits, total remaining $40.00):
- 19000000006  remaining $20.00  ends 2027-03-31
- 19000000007  remaining $20.00  ends 2026-10-31

Paused credits (1 credit, total remaining $20.00):
- 19000000007  Example Promotion for Security Customers  Paused  remaining $20.00  ends 2026-10-31  (cost category "OpenSharing" on rule "FirstOpen" with accounts: Backup Account 555555555555, Log Archive 666666666666 (sharing off))  [credit sharing DEACTIVATED for the owning account 666666666666]

Expiring in the next three months (1 credit, total remaining $20.00):
- 19000000003  remaining $20.00  ends 2026-12-31  (cost category "Credits-Sharing" on rule "Project-Alpha" with accounts: Operations 222222222222, Testing 444455556666)

CREDITS DASHBOARD SUMMARY
- Newly issued credits: 2 credits, total remaining $40.00
- Active credits: 2 credits, total remaining $40.00
- Paused: 1 credit, total remaining $20.00
- Exhausted in the last 3 months: 0 credits, total fully used 0.00
- Expiring this month: 0 credits, total remaining 0.00
- Expiring in the next three months: 1 credit, total remaining $20.00
- Expired in the last 3 months: 0 credits, total expired 0.00

Credits by sharing option
- cost-category-based:
   - Credits-Sharing (rule Project-Alpha) - 1 credit, total remaining $20.00
       accounts: Operations 222222222222, Testing 444455556666
- Shared by default: 0 credits, total remaining 0.00
- Sharing disabled: 1 credit, total remaining $20.00
   - 19000000006: sharing is off for its account Audit 333333333333
"""


def test_the_approved_layout_is_rendered_exactly():
    body = _body(CREDITS, preferences=StubPreferences())

    assert body == EXPECTED_LAYOUT


def test_without_preferences_members_are_not_marked_and_the_rest_is_unchanged():
    body = _body(CREDITS, preferences=None)

    assert "(sharing off)" not in body
    assert "Backup Account 555555555555, Log Archive 666666666666)" in body
    # The disabled state comes from creditSharingType, so it does not need the preferences.
    assert "[credit sharing DEACTIVATED for the owning account 666666666666]" in body
    assert "- 19000000006: sharing is off for its account Audit 333333333333" in body


def test_the_trailing_listing_returns_when_a_credit_expired_inside_the_window():
    recent = _credit("recent", "222222222222", "Recently expired", date(2026, 9, 30))
    body = _body(CREDITS + [recent], preferences=StubPreferences())

    assert body.rstrip().endswith('EXPIRED CREDITS IN THE LAST 3 MONTHS\n- 2026-09-30 - recent "Recently expired" - $20.00')


def test_the_overview_title_variants():
    assert overview_heading(PAYER, PAYER_NAME) == f"{OVERVIEW_HEADING} FOR PAYER {PAYER} ({PAYER_NAME})"
    assert overview_heading(PAYER, None) == f"{OVERVIEW_HEADING} FOR PAYER {PAYER}"
    assert overview_heading(None, None) == OVERVIEW_HEADING
    assert overview_heading(None, PAYER_NAME) == OVERVIEW_HEADING


def test_the_title_is_always_the_first_line():
    body = _body(CREDITS, preferences=StubPreferences(), name=None)

    assert body.splitlines()[0] == f"{OVERVIEW_HEADING} FOR PAYER {PAYER}"


# ---------------------------------------------------------------------------
# Orchestrator: payer name and the preferences-unavailable note
# ---------------------------------------------------------------------------


class FakeRetriever:
    def __init__(self, credits):
        self._credits = credits

    def retrieve(self):
        pages = [{"credits": [_raw(c) for c in self._credits]}]
        return RetrievalOutcome(raw_pages=pages, credits=parse_credits(pages))


class FakeStore:
    def write(self, snapshot):
        return "snapshots/test.json"

    def read_latest_prior(self, before_key=None):
        return None


class CapturingNotifier:
    def __init__(self):
        self.body = None

    def send(self, body):
        self.body = body
        return NotifyResult(sent=True, attempts=1)


class DownPreferences(CreditSharingPreferences):
    """Real adapter whose Billing call is denied."""

    def __init__(self):
        from botocore.exceptions import ClientError

        class Denied:
            def get_billing_preferences(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
                    "GetBillingPreferences",
                )

        super().__init__(Denied())


def _run(*, resolver=None, preferences=None, credits=None):
    notifier = CapturingNotifier()
    orchestrator = Orchestrator(
        credit_retriever=FakeRetriever(credits if credits is not None else [ALPHA_CREDIT]),
        snapshot_store=FakeStore(),
        notifier=notifier,
        definition_retriever=StubRetriever(),
        account_name_resolver=resolver,
        sharing_preferences=preferences,
        payer_account_id=PAYER,
    )
    result = orchestrator.run()
    return result, notifier.body


def test_the_payer_name_comes_from_organizations_first():
    result, body = _run(resolver=StubResolver({PAYER: PAYER_NAME}))

    assert result.payer_account_id == PAYER
    assert result.payer_account_name == PAYER_NAME
    assert body.splitlines()[0] == f"{OVERVIEW_HEADING} FOR PAYER {PAYER} ({PAYER_NAME})"


def test_the_payer_name_falls_back_to_the_preferences():
    class Prefs(StubPreferences):
        def account_name(self, account_id):
            return PAYER_NAME if account_id == PAYER else None

    result, body = _run(resolver=StubResolver({}), preferences=Prefs())

    assert result.payer_account_name == PAYER_NAME
    assert PAYER_NAME in body.splitlines()[0]


def test_the_title_has_no_name_when_nothing_resolves():
    result, body = _run(resolver=StubResolver({}))

    assert result.payer_account_name is None
    assert body.splitlines()[0] == f"{OVERVIEW_HEADING} FOR PAYER {PAYER}"


def test_unavailable_preferences_are_reported_once_and_do_not_fail_the_run():
    result, body = _run(resolver=StubResolver(), preferences=DownPreferences())

    assert result.success is True
    notes = [m for m in result.messages if m.startswith(SHARING_PREFERENCES_UNAVAILABLE_PREFIX)]
    assert len(notes) == 1
    assert "AccessDeniedException" in notes[0]
    # The report is still built; the members are just not marked.
    assert "(sharing off)" not in body
    assert "accounts: Operations 222222222222, Testing 444455556666" in body


def test_no_preferences_note_when_the_adapter_was_never_consulted():
    # No cost category credit and no disabled credit, and Organizations names the
    # payer: the preferences are never read.
    plain = _credit("plain", "222222222222", "Plain", date(2027, 1, 1))
    resolver = StubResolver({**ACCOUNT_NAMES, PAYER: PAYER_NAME})
    result, _ = _run(resolver=resolver, preferences=DownPreferences(), credits=[plain])

    assert not [m for m in result.messages if m.startswith(SHARING_PREFERENCES_UNAVAILABLE_PREFIX)]
