"""Task 16.6: property and unit tests for Lifecycle_Status, the Active/Inactive
partition, and the email's INACTIVE CREDITS section.

This module complements the existing partition-focused tests in
``test_lifecycle_analyzer.py`` (which cover the totality of
``LifecycleAnalyzer.partition``) by pinning the *semantic* guarantees of the
pure ``lifecycle_status`` function and the dashboard/email surfaces that
consume it.

Property 9 (this task's numbering): for any credit and any ``as_of`` date,
``lifecycle_status`` returns exactly one value; it is Expired iff ``endDate``
is present and strictly before ``as_of``; it is never Expired when ``endDate``
is absent; an Inactive credit never appears in an expiration tier; and
``active_count + inactive_count`` equals the credit count.
Validates: Requirements 4.7, 7.3, 7.9, 8.1, 8.2, 8.3, 8.5.

Property 12: for any credits and snapshot date, every Inactive_Credit is listed
exactly once in the email body: Paused (Disabled) credits in the "Paused credits"
alert, Expired and Exhausted credits in the trailing listing. The trailing
listing appears at most once and only when there is something to list.
Validates: Requirements 8.9, 8.10, 12.3.

Everything runs through the real analysis pipeline (Lifecycle_Analyzer,
Expiration_Analyzer, New_Credit_Detector, build_dashboard, build_dashboard_body)
so the generated inputs stay internally consistent. No AWS
client is constructed anywhere in this module.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from aws_credits_reporter.dashboard import INACTIVE_STATUSES, build_dashboard
from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
from aws_credits_reporter.models import (
    Credit,
    CurrencyAmount,
    LifecycleStatus,
    Snapshot,
    lifecycle_status,
    parse_credits,
)
from aws_credits_reporter.new_credit_detector import NewCreditDetector
from aws_credits_reporter.report_body import EXPIRED_CREDITS_HEADING, INACTIVE_CREDITS_HEADING, PAUSED_CREDITS_ALERT_HEADING, _status_label, build_dashboard_body
FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_DATE = date(2026, 3, 12)


def _load(name: str) -> Credit:
    page = json.loads((FIXTURES / name).read_text())
    [credit] = parse_credits([page])
    return credit


# --------------------------------------------------------------------------
# Property 9 (semantic): lifecycle_status is single-valued with the exact
# Expired semantics, and its downstream consumers honor the partition.
#
# Validates: Requirements 4.7, 7.3, 7.9, 8.1, 8.2, 8.3, 8.5
# --------------------------------------------------------------------------

_ALL_STATUSES = frozenset(LifecycleStatus)

_dates = st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31))
_optional_dates = st.one_of(st.none(), _dates)
_optional_amount = st.one_of(
    st.none(),
    st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("100000"),
        allow_nan=False,
        allow_infinity=False,
        places=2,
    ).map(lambda amount: CurrencyAmount(Decimal(amount), "USD")),
)
_credit_status = st.sampled_from([None, "ENABLED", "DISABLED"])
_credit_ids = st.one_of(st.none(), st.text(min_size=1, max_size=8))


@st.composite
def _credit(draw: st.DrawFn) -> Credit:
    return Credit(
        creditId=draw(_credit_ids),
        endDate=draw(_optional_dates),
        exhaustDate=draw(_optional_dates),
        remainingAmount=draw(_optional_amount),
        creditStatus=draw(_credit_status),
    )


@settings(max_examples=200)
@given(credit=_credit(), as_of=_dates)
def test_property9_lifecycle_status_is_single_valued_with_expired_semantics(
    credit: Credit, as_of: date
) -> None:
    """`lifecycle_status` returns exactly one value with exact Expired rules.

    Validates: Requirements 4.7, 8.1, 8.2, 8.3
    """
    status = lifecycle_status(credit, as_of)

    # Exactly one of the four enum values is returned.
    assert status in _ALL_STATUSES

    # Expired iff endDate is present and strictly before as_of (Req 8.1(a), 8.3).
    is_expired = status is LifecycleStatus.EXPIRED
    should_be_expired = credit.endDate is not None and credit.endDate < as_of
    assert is_expired == should_be_expired

    # Never Expired when endDate is absent (Req 8.3).
    if credit.endDate is None:
        assert status is not LifecycleStatus.EXPIRED

    # endDate == as_of is not Expired (rule (a) is strict less-than).
    if credit.endDate == as_of:
        assert status is not LifecycleStatus.EXPIRED


@settings(max_examples=200)
@given(credits=st.lists(_credit(), max_size=20), as_of=_dates)
def test_property9_inactive_never_in_a_tier_and_counts_reconcile(
    credits: list[Credit], as_of: date
) -> None:
    """Inactive credits never enter an expiration tier; counts reconcile.

    Validates: Requirements 7.3, 7.9, 8.2, 8.5
    """
    now_utc = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc)
    lifecycle = LifecycleAnalyzer(as_of).partition(credits)

    # active_count + inactive_count equals the credit count (Req 8.2, 8.5).
    assert len(lifecycle.active) + len(lifecycle.inactive) == len(credits)

    # The Expiration_Analyzer receives only the Active partition, so no
    # Inactive_Credit can ever land in a tier (Req 7.3, 7.9).
    expiration = ExpirationAnalyzer(now_utc).classify(lifecycle.active)
    tier_members = list(expiration.this_month.credits) + list(
        expiration.next_three_months.credits
    )
    tier_member_ids = {id(c) for c in tier_members}
    inactive_ids = {id(c) for c in lifecycle.inactive}
    assert tier_member_ids.isdisjoint(inactive_ids)

    # Every tier member is Active at the Snapshot_Date.
    for credit in tier_members:
        assert lifecycle_status(credit, as_of) is LifecycleStatus.ACTIVE

    # Even if the tiers were (incorrectly) fed the full credit list, an
    # Inactive credit is still refused a tier by the analyzer's own guard.
    expiration_all = ExpirationAnalyzer(now_utc).classify(credits)
    all_tier_members = list(expiration_all.this_month.credits) + list(
        expiration_all.next_three_months.credits
    )
    for credit in all_tier_members:
        assert lifecycle_status(credit, as_of) is LifecycleStatus.ACTIVE


# --------------------------------------------------------------------------
# Unit tests: the Active/Inactive partition (Req 4.8, 4.9, 4.10, 7.3, 8.6, 8.7).
# --------------------------------------------------------------------------


def _snapshot_of(credits: list[Credit], taken_at: datetime) -> Snapshot:
    def raw(credit: Credit) -> dict:
        remaining = credit.remainingAmount
        return {
            "creditId": credit.creditId,
            "remainingAmount": (
                {"currencyAmount": str(remaining.amount), "currencyCode": remaining.currency}
                if remaining is not None
                else None
            ),
            "startDate": credit.startDate.isoformat() if credit.startDate else None,
            "endDate": credit.endDate.isoformat() if credit.endDate else None,
            "creditStatus": credit.creditStatus,
            "exhaustDate": credit.exhaustDate.isoformat() if credit.exhaustDate else None,
        }

    return Snapshot(
        key="k",
        taken_at=taken_at,
        raw_get_credits=[{"credits": [raw(c) for c in credits]}],
    )


def test_expired_but_enabled_credit_is_inactive_and_absent_from_active_and_tiers() -> None:
    """Expired-but-ENABLED: Inactive table, not the active total, not any tier."""
    expired = _load("get_credits_expired_enabled.json")  # endDate 2025-06-30, ENABLED
    active = Credit(
        creditId="c-active",
        endDate=date(2026, 3, 31),
        remainingAmount=CurrencyAmount(Decimal("100"), "USD"),
        creditStatus="ENABLED",
    )
    now_utc = datetime(2026, 3, 12, tzinfo=timezone.utc)
    credits = [expired, active]

    lifecycle = LifecycleAnalyzer(SNAPSHOT_DATE).partition(credits)
    assert expired in lifecycle.inactive
    assert expired not in lifecycle.active

    # Not in the active total: active total is only the active credit's amount.
    dashboard = build_dashboard(
        lifecycle,
        ExpirationAnalyzer(now_utc).classify(lifecycle.active),
        NewCreditDetector().detect(_snapshot_of(credits, now_utc), None),
    )
    assert dashboard.active_count == 1
    assert dashboard.active_total == Decimal("100")

    # Not in any tier (its endDate is well in the past and it is Inactive).
    expiration = ExpirationAnalyzer(now_utc).classify(lifecycle.active)
    assert expired.creditId not in expiration.this_month.credit_ids
    assert expired.creditId not in expiration.next_three_months.credit_ids
    assert lifecycle_status(expired, SNAPSHOT_DATE) is LifecycleStatus.EXPIRED


def test_exhausted_credit_with_zero_remaining_is_exhausted_status() -> None:
    exhausted = _load("get_credits_exhausted.json")  # remaining 0, ENABLED
    assert lifecycle_status(exhausted, SNAPSHOT_DATE) is LifecycleStatus.EXHAUSTED



def test_disabled_credit_is_disabled_status() -> None:
    disabled = _load("get_credits_disabled.json")  # future endDate, balance, DISABLED
    assert lifecycle_status(disabled, SNAPSHOT_DATE) is LifecycleStatus.DISABLED



def test_end_date_equal_to_as_of_is_active() -> None:
    boundary = Credit(
        creditId="boundary",
        endDate=SNAPSHOT_DATE,
        remainingAmount=CurrencyAmount(Decimal("10"), "USD"),
        creditStatus="ENABLED",
    )
    assert lifecycle_status(boundary, SNAPSHOT_DATE) is LifecycleStatus.ACTIVE



# --------------------------------------------------------------------------
# Email body: the INACTIVE CREDITS section.
# --------------------------------------------------------------------------

NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
AS_OF = NOW.date()

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


def _build_body_and_lifecycle(current: list[Credit]):
    """Return ``(body, lifecycle)`` built through the real pipeline."""
    lifecycle = LifecycleAnalyzer(AS_OF).partition(current)
    expiration = ExpirationAnalyzer(NOW).classify(lifecycle.active)
    new = NewCreditDetector().detect(_snapshot("current", current), None)
    dashboard = build_dashboard(lifecycle, expiration, new)
    body = build_dashboard_body(dashboard, expiration, new, lifecycle.inactive)
    return body, lifecycle


def _inactive_listing_heading(body: str) -> str | None:
    """The heading of the trailing listing, or ``None`` when the block is omitted."""
    if EXPIRED_CREDITS_HEADING in body:
        return EXPIRED_CREDITS_HEADING
    if INACTIVE_CREDITS_HEADING in body:
        return INACTIVE_CREDITS_HEADING
    return None


def _inactive_block(body: str) -> str:
    """The trailing listing from its heading to the end; empty when omitted."""
    heading = _inactive_listing_heading(body)
    return body[body.index(heading):] if heading is not None else ""


# The email body labels the Disabled Lifecycle_Status group "Paused",
# so the group labels the parser skips are the display labels, not the enum
# values.
_GROUP_LABELS = {_status_label(status) for status in INACTIVE_STATUSES}


def _bullet_credit_id(line: str) -> str:
    body_text = line[2:].strip()
    # Two formats: grouped INACTIVE "<creditId>  <desc> ..." and the
    # compact all-expired "<date> - <creditId> "<desc>" - <amount>",
    # the latter identifiable by its quoted description.
    if '"' in body_text and " - " in body_text:
        return body_text.split(" - ", 1)[1].split()[0]
    return body_text.split()[0]


def _listed_inactive_ids(body: str) -> list[str]:
    """creditIds listed in the trailing listing (bullet lines); empty when omitted."""
    lines = _inactive_block(body).splitlines()[1:]  # skip the heading itself
    return [
        _bullet_credit_id(line)
        for line in lines
        if line.startswith("- ")
    ]


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


# Property 12: the INACTIVE CREDITS section lists exactly the Inactive_Credits
# and appears exactly once.
# Validates: Requirements 8.9, 8.10, 12.3


def _inactive_body_credit(index: int) -> st.SearchStrategy[Credit]:
    """A credit that can land in any lifecycle bucket, with a stable creditId."""
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
        exhaustDate=st.one_of(st.none(), st.dates(min_value=date(2024, 1, 1), max_value=date(2027, 12, 31))),
        creditStatus=st.sampled_from([None, "ENABLED", "DISABLED"]),
    )


@st.composite
def _inactive_body_credits(draw: st.DrawFn) -> list[Credit]:
    n = draw(st.integers(min_value=0, max_value=8))
    return [draw(_inactive_body_credit(i)) for i in range(n)]


@settings(max_examples=100)
@given(current=_inactive_body_credits())
def test_property12_every_inactive_credit_is_listed_exactly_once(current):
    """Paused credits sit in the alert; Expired and Exhausted in the trailing listing.

    Validates: Requirements 8.9, 8.10, 12.3
    """
    body, lifecycle = _build_body_and_lifecycle(current)

    # The trailing listing appears at most once, under one heading ("EXPIRED
    # CREDITS" when every listed credit is Expired, else "INACTIVE CREDITS").
    heading = _inactive_listing_heading(body)
    if heading is not None:
        assert body.count(heading) == 1
        other = (
            INACTIVE_CREDITS_HEADING
            if heading == EXPIRED_CREDITS_HEADING
            else EXPIRED_CREDITS_HEADING
        )
        assert other not in body

    paused_ids = {
        c.creditId
        for c in lifecycle.inactive
        if c.creditId is not None
        and lifecycle.status_by_id[c.creditId] is LifecycleStatus.DISABLED
    }
    expired_or_exhausted_ids = {
        c.creditId for c in lifecycle.inactive if c.creditId is not None
    } - paused_ids

    assert set(_paused_alert_ids(body)) == paused_ids
    assert len(_paused_alert_ids(body)) == len(paused_ids)
    assert set(_listed_inactive_ids(body)) == expired_or_exhausted_ids
    assert len(_listed_inactive_ids(body)) == len(expired_or_exhausted_ids)

    # The trailing listing is omitted (no empty-state sentence) when there is
    # nothing to list.
    assert (heading is None) == (not expired_or_exhausted_ids)


def _credit(credit_id, remaining, end, status="ENABLED", exhaust=None, description=None):
    return Credit(
        creditId=credit_id,
        remainingAmount=CurrencyAmount(Decimal(remaining), "USD") if remaining is not None else None,
        endDate=end,
        creditStatus=status,
        exhaustDate=exhaust,
        description=description,
    )


# One credit per Inactive bucket plus an Active credit, at AS_OF 2026-05-15.
EXPIRED = _credit("c-expired", "400.00", date(2025, 3, 31), description="Migration program")
EXHAUSTED = _credit("c-exhausted", "0.00", date(2026, 12, 31))
DISABLED = _credit("c-disabled", "2000.00", date(2027, 1, 1), status="DISABLED")
ACTIVE = _credit("c-active", "18750.00", date(2026, 9, 30))
MIXED = [ACTIVE, EXPIRED, EXHAUSTED, DISABLED]


def test_trailing_listing_is_present_only_when_something_is_listable() -> None:
    # Expired and Exhausted credits: the listing is present once.
    body, _ = _build_body_and_lifecycle(MIXED)
    assert body.count(INACTIVE_CREDITS_HEADING) == 1
    # No credits at all: the trailing listing is omitted entirely.
    empty_body, _ = _build_body_and_lifecycle([])
    assert INACTIVE_CREDITS_HEADING not in empty_body
    assert EXPIRED_CREDITS_HEADING not in empty_body


def test_inactive_listing_holds_expired_and_exhausted_and_paused_is_in_the_alerts() -> None:
    body, lifecycle = _build_body_and_lifecycle(MIXED)
    block = _inactive_block(body)

    # The trailing listing has the Expired and Exhausted credits only.
    assert set(_listed_inactive_ids(body)) == {"c-expired", "c-exhausted"}
    assert "c-active" not in block
    assert "c-disabled" not in block

    # The Paused credit is listed in the "Paused credits" alert instead.
    assert _paused_alert_ids(body) == ["c-disabled"]

    # MIXED has an Exhausted credit, so not every listed credit is Expired:
    # the listing is titled "INACTIVE CREDITS".
    assert _inactive_listing_heading(body) == INACTIVE_CREDITS_HEADING

    # Group labels appear in the order Expired, Exhausted.
    expired_pos = block.index(_status_label(LifecycleStatus.EXPIRED))
    exhausted_pos = block.index(_status_label(LifecycleStatus.EXHAUSTED))
    assert expired_pos < exhausted_pos
    # The enum value "Disabled" never appears in the email body: the label is "Paused".
    assert LifecycleStatus.DISABLED.value not in body
    assert "Paused" in body


def test_no_trailing_listing_and_no_paused_alert_when_nothing_is_inactive() -> None:
    # A single Active credit -> no inactive credits -> nothing to list.
    body, lifecycle = _build_body_and_lifecycle([ACTIVE])
    assert lifecycle.inactive == []
    assert _inactive_listing_heading(body) is None
    assert _listed_inactive_ids(body) == []
    assert _paused_alert_ids(body) == []
    assert PAUSED_CREDITS_ALERT_HEADING not in body


def test_published_message_still_contains_inactive_block() -> None:
    """Req 12.3: the SNS publish keeps the INACTIVE CREDITS block."""
    from aws_credits_reporter.notifier import Notifier

    body, _ = _build_body_and_lifecycle(MIXED)

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
    assert body_text == body
    assert INACTIVE_CREDITS_HEADING in body_text
    assert PAUSED_CREDITS_ALERT_HEADING in body_text
    assert "c-expired" in body_text
    assert "c-exhausted" in body_text
    assert "c-disabled" in body_text
    assert "ttachment" not in body_text


def test_inactive_by_status_counts_sum_to_inactive_count_and_exhausted_total_is_zero() -> None:
    """`inactive_by_status` counts sum to `inactive_count`; Exhausted total is zero (Req 8.8)."""
    lifecycle = LifecycleAnalyzer(AS_OF).partition(MIXED)
    expiration = ExpirationAnalyzer(NOW).classify(lifecycle.active)
    new = NewCreditDetector().detect(_snapshot("current", MIXED), None)
    dashboard = build_dashboard(lifecycle, expiration, new)

    total = sum(dashboard.inactive_by_status[status].count for status in INACTIVE_STATUSES)
    assert total == dashboard.inactive_count

    # The Exhausted total is always zero by definition of rule (b): a credit is
    # Exhausted only when its remaining balance is zero (or it has an
    # exhaustDate, which the console pairs with a zero balance).
    assert dashboard.inactive_by_status[LifecycleStatus.EXHAUSTED].total_remaining == Decimal("0")
