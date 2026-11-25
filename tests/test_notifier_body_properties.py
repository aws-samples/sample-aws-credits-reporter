"""Task 13.2: unit and property tests for the enriched Dashboard_Summary body.

Covers the expiration-alert behavior of ``build_dashboard_body`` (Req 7.6,
7.7, 7.8) and the layout guarantees that the alerts precede the dashboard
section and survive the SNS publish unchanged (Req 12.2, 12.3).

Property 9 (this task): for any expiration result the body carries an
expiration alert if and only if at least one tier is non-empty, and every
credit listed in an expiration alert belongs to a tier.

The body is built through the real analysis pipeline (Expiration_Analyzer,
Lifecycle_Analyzer, New_Credit_Detector, build_dashboard) so the generated
inputs stay internally consistent, exactly as the task-13.1 tests do. No AWS
client is constructed anywhere in this module.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from aws_credits_reporter.dashboard import build_dashboard
from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
from aws_credits_reporter.models import Credit, CurrencyAmount, Snapshot
from aws_credits_reporter.new_credit_detector import NewCreditDetector
from aws_credits_reporter.report_body import OVERVIEW_HEADING, DASHBOARD_SUMMARY_HEADING, EXPIRING_NEXT_THREE_HEADING, EXPIRING_THIS_MONTH_HEADING, build_dashboard_body

# A fixed "now" mid-month so the current-month and next-three-month windows
# are unambiguous and month boundaries are exercised by the generated dates.
NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
AS_OF = NOW.date()

# --------------------------------------------------------------------------
# Pipeline helpers (mirror the task-13.1 tests so inputs stay consistent)
# --------------------------------------------------------------------------


def _raw(credit: Credit) -> dict:
    remaining = credit.remainingAmount
    return {
        "creditId": credit.creditId,
        "remainingAmount": (
            {"currencyAmount": str(remaining.amount), "currencyCode": remaining.currency}
            if remaining is not None
            else None
        ),
        "endDate": credit.endDate.isoformat() if credit.endDate is not None else None,
        "creditStatus": credit.creditStatus or "ENABLED",
    }


def _snapshot(key: str, credits: list[Credit]) -> Snapshot:
    return Snapshot(key=key, taken_at=NOW, raw_get_credits=[{"credits": [_raw(c) for c in credits]}])


def _build_body(current: list[Credit], prior_ids: list[str] | None):
    """Return ``(body, expiration_result)`` built through the real pipeline."""
    expiration = ExpirationAnalyzer(NOW).classify(current)
    prior = _snapshot("prior", [Credit(creditId=i) for i in prior_ids]) if prior_ids is not None else None
    new = NewCreditDetector().detect(_snapshot("current", current), prior, prior_read_ok=True)
    lifecycle = LifecycleAnalyzer(AS_OF).partition(current)
    dashboard = build_dashboard(lifecycle, expiration, new)
    body = build_dashboard_body(dashboard, expiration, new, lifecycle.inactive)
    return body, expiration


def _alerts_block(body: str) -> str:
    """The portion of the body above the DASHBOARD SUMMARY heading."""
    return body[: body.index(DASHBOARD_SUMMARY_HEADING)]


def _tier_credit_ids(body: str, heading_prefix: str) -> list[str]:
    """creditIds listed in the alert subsection whose line starts with ``heading_prefix``.

    Returns an empty list when that subsection is not present.
    """
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(heading_prefix):
            start = i
            break
    if start is None:
        return []
    ids: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("- "):
            break
        ids.append(line[2:].strip().split()[0])
    return ids


# --------------------------------------------------------------------------
# Property 9: expiration alert presence iff a tier is non-empty
# --------------------------------------------------------------------------

# Feature: aws-credits-reporter, Property 9: the email body carries an expiration
# alert iff at least one tier is non-empty, and every listed credit is a tier
# member.
# Validates: Requirements 7.6, 7.7, 7.8


def _credit_strategy(index: int) -> st.SearchStrategy[Credit]:
    """An active-eligible Credit with a date spanning well before/after the windows.

    endDate ranges from three months before "now" to eight months after, so
    generated credits land in the current month, the next-three-months tier,
    or outside both. Status stays ENABLED and remaining amounts stay positive
    so the credit is Active (an Inactive credit never enters a tier, which
    would confound the property under test).
    """
    return st.builds(
        Credit,
        creditId=st.just(f"c-{index}"),
        remainingAmount=st.builds(
            CurrencyAmount,
            amount=st.decimals(min_value=Decimal("1"), max_value=Decimal("100000"), places=2),
            currency=st.just("USD"),
        ),
        endDate=st.one_of(
            st.none(),
            st.dates(min_value=date(2026, 2, 1), max_value=date(2027, 1, 31)),
        ),
        creditStatus=st.just("ENABLED"),
    )


@st.composite
def _credits(draw: st.DrawFn) -> list[Credit]:
    n = draw(st.integers(min_value=0, max_value=8))
    return [draw(_credit_strategy(i)) for i in range(n)]


@settings(max_examples=100)
@given(current=_credits())
def test_property9_expiration_alert_iff_tier_nonempty_and_members_belong(current):
    body, expiration = _build_body(current, prior_ids=None)

    this_ids = {c.creditId for c in expiration.this_month.credits}
    next_ids = {c.creditId for c in expiration.next_three_months.credits}
    any_tier_nonempty = bool(this_ids) or bool(next_ids)

    alerts = _alerts_block(body)
    this_heading = f"{EXPIRING_THIS_MONTH_HEADING} ("
    next_heading = f"{EXPIRING_NEXT_THREE_HEADING} ("
    has_expiration_alert = (this_heading in alerts) or (next_heading in alerts)

    # Presence iff at least one tier is non-empty (Req 7.6, 7.7, 7.8).
    assert has_expiration_alert == any_tier_nonempty

    # A tier subsection is present exactly when that tier has members.
    assert (this_heading in alerts) == bool(this_ids)
    assert (next_heading in alerts) == bool(next_ids)

    # Every credit listed under a tier is a member of that tier, and the
    # listed set equals the tier membership (no member dropped, none invented).
    assert set(_tier_credit_ids(body, this_heading)) == this_ids
    assert set(_tier_credit_ids(body, next_heading)) == next_ids

    # No credit outside both tiers ever appears in an expiration alert.
    tier_members = this_ids | next_ids
    for c in current:
        if c.creditId not in tier_members:
            listed_this = _tier_credit_ids(body, this_heading)
            listed_next = _tier_credit_ids(body, next_heading)
            assert c.creditId not in listed_this
            assert c.creditId not in listed_next


# --------------------------------------------------------------------------
# Unit: layout and the SNS publish
# --------------------------------------------------------------------------


def _credit(credit_id: str, remaining: str, end: date, status: str = "ENABLED") -> Credit:
    return Credit(
        creditId=credit_id,
        remainingAmount=CurrencyAmount(Decimal(remaining), "USD"),
        endDate=end,
        creditStatus=status,
    )


# c-this expires this month; c-next in the next-three window; c-new is issued
# since the prior run.
THIS_MONTH = _credit("c-this", "120.50", date(2026, 5, 28))
NEXT_THREE = _credit("c-next", "300.00", date(2026, 7, 15))
NEW = _credit("c-new", "42.00", date(2026, 12, 31))
CURRENT = [THIS_MONTH, NEXT_THREE, NEW]


def test_alerts_appear_before_the_dashboard_section():
    body, _ = _build_body(CURRENT, prior_ids=["c-this", "c-next"])

    assert OVERVIEW_HEADING in body
    assert DASHBOARD_SUMMARY_HEADING in body
    assert body.index(OVERVIEW_HEADING) < body.index(DASHBOARD_SUMMARY_HEADING)
    # Both expiration alerts sit inside the alerts block, above the summary.
    alerts = _alerts_block(body)
    assert f"{EXPIRING_THIS_MONTH_HEADING} (" in alerts
    assert f"{EXPIRING_NEXT_THREE_HEADING} (" in alerts
    # This-month subsection precedes the next-three subsection (Req 7.6, 7.7).
    assert body.index(f"{EXPIRING_THIS_MONTH_HEADING} (") < body.index(
        f"{EXPIRING_NEXT_THREE_HEADING} ("
    )


def test_published_email_still_contains_alerts_and_dashboard():
    from aws_credits_reporter.notifier import Notifier

    body, _ = _build_body(CURRENT, prior_ids=["c-this", "c-next"])

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
    message = client.calls[-1]["Message"]
    # The full body is published verbatim, with nothing appended.
    assert message == body
    assert OVERVIEW_HEADING in message
    assert DASHBOARD_SUMMARY_HEADING in message
    assert f"{EXPIRING_THIS_MONTH_HEADING} (" in message
    assert "c-this" in message and "c-next" in message
    assert "ttachment" not in message
