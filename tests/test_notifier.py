"""Send adapter with retry and Dashboard_Summary body tests for ``aws_credits_reporter.notifier``.

SNS is the only delivery backend and the email body is the only deliverable.
Pure in-memory checks; the notifier is exercised against a fake client that
records calls, so no AWS client is constructed anywhere in this module.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

from aws_credits_reporter.dashboard import (
    BASELINE_NOTICE,
    COMPARISON_ERROR_NOTICE,
    NO_ACTIVE_CREDITS_MESSAGE,
    NO_NEW_CREDITS_MESSAGE,
    build_dashboard,
)
from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
from aws_credits_reporter.models import Credit, CurrencyAmount, Snapshot
from aws_credits_reporter.new_credit_detector import NewCreditDetector
from aws_credits_reporter.notifier import Notifier, NotifyResult
from aws_credits_reporter.report_body import OVERVIEW_HEADING, DASHBOARD_SUMMARY_HEADING, EXPIRING_NEXT_THREE_HEADING, EXPIRING_THIS_MONTH_HEADING, NEW_CREDITS_ALERT_HEADING, NOT_AVAILABLE, build_dashboard_body, format_credit_alert_bullet

TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:credits"


# --------------------------------------------------------------------------
# Send adapter with retry
# --------------------------------------------------------------------------


def _client_error(code: str = "Throttling", message: str = "Rate exceeded") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": 400}},
        "Publish",
    )


class FakeClient:
    """Records ``publish`` calls; the first ``failures`` calls raise."""

    def __init__(self, failures: int = 0, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._failures = failures
        self._error = error or _client_error()

    def publish(self, **kwargs):
        self.calls.append(("publish", kwargs))
        if len(self.calls) <= self._failures:
            raise self._error
        return {"MessageId": f"m-{len(self.calls)}"}


class FakeClock:
    """Monotonic clock advanced by ``tick`` on every read and by the slept amount."""

    def __init__(self, tick: float = 0.0) -> None:
        self.now = 1000.0
        self._tick = tick
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        self.now += self._tick
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _notifier(client, **overrides) -> Notifier:
    clock = overrides.pop("clock", FakeClock())
    defaults = dict(
        sns_topic_arn=TOPIC_ARN,
        sleep=clock.sleep,
        clock=clock,
    )
    defaults.update(overrides)
    return Notifier(client, **defaults)


BODY = "AWS Credits Reporter run summary\nTotal credits: 2\nTotal remaining: 500.00 USD\n"


def test_no_topic_arn_records_config_error_and_makes_zero_calls():
    client = FakeClient()
    result = _notifier(client, sns_topic_arn=None).send(BODY)

    assert isinstance(result, NotifyResult)
    assert result.config_error is not None
    assert "topic ARN" in result.config_error
    assert result.sent is False
    assert result.delivery_failed is False
    assert result.attempts == 0
    assert client.calls == []
    assert any("configuration error" in m for m in result.messages)


def test_missing_client_is_rejected_unless_dry_run():
    with pytest.raises(ValueError):
        Notifier(None, sns_topic_arn=TOPIC_ARN)
    Notifier(None, sns_topic_arn=TOPIC_ARN, dry_run=True)


def test_success_on_first_attempt_publishes_the_body_to_the_topic():
    client = FakeClient()
    clock = FakeClock()
    result = _notifier(client, clock=clock).send(BODY)

    assert result.sent is True
    assert result.attempts == 1
    assert result.delivery_failed is False
    assert result.config_error is None
    assert clock.sleeps == []

    assert len(client.calls) == 1
    name, kwargs = client.calls[0]
    assert name == "publish"
    assert kwargs["TopicArn"] == TOPIC_ARN
    assert len(kwargs["Subject"]) <= 100
    assert kwargs["Message"] == BODY
    assert set(kwargs) == {"TopicArn", "Subject", "Message"}
    assert any("notification sent" in m for m in result.messages)
    assert not any("ttachment" in m for m in result.messages)


def test_subject_is_truncated_to_the_sns_limit():
    client = FakeClient()
    _notifier(client, subject="s" * 250).send(BODY)
    assert len(client.calls[0][1]["Subject"]) == 100


def test_success_on_second_attempt_after_one_failure_backs_off_once():
    client = FakeClient(failures=1)
    clock = FakeClock()
    result = _notifier(client, clock=clock).send(BODY)

    assert result.sent is True
    assert result.attempts == 2
    assert result.delivery_failed is False
    assert result.failure_reason is None
    assert len(client.calls) == 2
    assert clock.sleeps == [0.5]
    assert any("attempt 2" in m for m in result.messages)


def test_three_failures_record_delivery_failure_with_reason():
    client = FakeClient(failures=3, error=_client_error("AuthorizationError", "Not authorized to publish"))
    clock = FakeClock()
    result = _notifier(client, clock=clock).send(BODY)

    assert result.sent is False
    assert result.delivery_failed is True
    assert result.attempts == 3
    assert len(client.calls) == 3
    assert clock.sleeps == [0.5, 1.0]
    assert result.failure_reason is not None
    assert "3 attempt" in result.failure_reason
    assert "AuthorizationError" in result.failure_reason
    assert "Not authorized to publish" in result.failure_reason
    assert any("delivery failure" in m for m in result.messages)


def test_non_client_exceptions_are_also_recorded_not_raised():
    client = FakeClient(failures=3, error=RuntimeError("socket closed"))
    result = _notifier(client).send(BODY)

    assert result.delivery_failed is True
    assert "RuntimeError: socket closed" in (result.failure_reason or "")


def test_send_deadline_stops_retrying_early():
    # Every clock read advances 300 s: the deadline is taken at the start read,
    # and the read after the first failed attempt already sits on it, so the
    # backoff would cross the deadline and no further call is made.
    client = FakeClient(failures=3)
    clock = FakeClock(tick=300.0)
    result = _notifier(client, clock=clock, send_deadline_s=300).send(BODY)

    assert result.sent is False
    assert result.delivery_failed is True
    assert result.attempts == 1
    assert len(client.calls) == 1
    assert clock.sleeps == []
    assert "deadline" in (result.failure_reason or "")


def test_dry_run_does_not_call_the_backend():
    client = FakeClient()
    result = _notifier(client, dry_run=True).send(BODY)

    assert result.dry_run is True
    assert result.sent is False
    assert result.attempts == 0
    assert client.calls == []
    assert any("dry run: notification not sent" in m for m in result.messages)


def test_dry_run_still_reports_a_missing_topic():
    result = _notifier(None, sns_topic_arn=None, dry_run=True).send(BODY)
    assert result.config_error is not None
    assert result.dry_run is False


# --------------------------------------------------------------------------
# Task 13.1: Dashboard_Summary body with alerts
# --------------------------------------------------------------------------

NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _credit(
    credit_id: str,
    remaining: str | None,
    end: date | None,
    status: str = "ENABLED",
    currency: str = "USD",
) -> Credit:
    return Credit(
        creditId=credit_id,
        remainingAmount=(
            CurrencyAmount(Decimal(remaining), currency) if remaining is not None else None
        ),
        endDate=end,
        creditStatus=status,
    )


def _raw(credit_id: str, remaining: str, end: str, status: str = "ENABLED") -> dict:
    return {
        "creditId": credit_id,
        "remainingAmount": {"currencyAmount": remaining, "currencyCode": "USD"},
        "endDate": end,
        "creditStatus": status,
    }


def _snapshot(key: str, raws: list[dict]) -> Snapshot:
    return Snapshot(key=key, taken_at=NOW, raw_get_credits=[{"credits": raws}])


# Fixed scenario: c-this expires this month, c-next in three months, c-far is
# outside both tiers, c-new is present now but not in the prior snapshot.
THIS_MONTH = [_credit("c-this", "120.5", date(2026, 5, 28))]
NEXT_THREE = [
    _credit("c-next-a", "300", date(2026, 7, 1)),
    _credit("c-next-b", None, date(2026, 8, 31)),
]
FAR = [_credit("c-far", "1000", date(2027, 1, 1))]
NEW = [_credit("c-new", "42", date(2026, 12, 31))]
CURRENT = THIS_MONTH + NEXT_THREE + FAR + NEW


def _new_result(current: list[Credit], prior_ids: list[str] | None, prior_ok: bool = True):
    current_raw = [
        _raw(c.creditId, str(c.remainingAmount.amount) if c.remainingAmount else "0",
             c.endDate.isoformat() if c.endDate else "2030-01-01", c.creditStatus or "ENABLED")
        for c in current
    ]
    prior = None
    if prior_ids is not None:
        prior = _snapshot("prior", [_raw(i, "1", "2030-01-01") for i in prior_ids])
    return NewCreditDetector().detect(_snapshot("current", current_raw), prior, prior_read_ok=prior_ok)


def _body(current: list[Credit], prior_ids: list[str] | None, prior_ok: bool = True) -> str:
    expiration = ExpirationAnalyzer(NOW).classify(current)
    new = _new_result(current, prior_ids, prior_ok)
    lifecycle = LifecycleAnalyzer(NOW.date()).partition(current)
    dashboard = build_dashboard(lifecycle, expiration, new)
    return build_dashboard_body(dashboard, expiration, new, lifecycle.inactive)


FULL_BODY = _body(CURRENT, prior_ids=["c-this", "c-next-a", "c-next-b", "c-far"])


def _lines_under(body: str, heading_prefix: str) -> list[str]:
    """The bullet credit lines directly following the section whose line starts with ``heading_prefix``."""
    lines = body.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading_prefix))
    collected = []
    for line in lines[start + 1 :]:
        if not line.startswith("- "):
            break
        collected.append(line[2:].strip())
    return collected


def test_alerts_come_before_the_dashboard_summary():
    assert OVERVIEW_HEADING in FULL_BODY
    assert DASHBOARD_SUMMARY_HEADING in FULL_BODY
    assert FULL_BODY.index(OVERVIEW_HEADING) < FULL_BODY.index(DASHBOARD_SUMMARY_HEADING)
    assert FULL_BODY.index(NEW_CREDITS_ALERT_HEADING) < FULL_BODY.index(DASHBOARD_SUMMARY_HEADING)
    # Alerts subsections appear in the documented order: new, this month, next three.
    assert (
        FULL_BODY.index(f"{NEW_CREDITS_ALERT_HEADING} (")
        < FULL_BODY.index(f"{EXPIRING_THIS_MONTH_HEADING} (")
        < FULL_BODY.index(f"{EXPIRING_NEXT_THREE_HEADING} (")
    )


def test_new_credit_alert_lists_each_new_credit_id_with_remaining_and_end_date():
    listed = _lines_under(FULL_BODY, f"{NEW_CREDITS_ALERT_HEADING} (")
    assert len(listed) == 1
    assert listed[0].startswith("c-new")
    assert "$42.00" in listed[0]
    assert "2026-12-31" in listed[0]
    assert f"{NEW_CREDITS_ALERT_HEADING} (1 credit, total remaining $42.00):" in FULL_BODY


def test_expiration_alerts_list_only_the_tier_members_with_remaining_and_end_date():
    this_month = _lines_under(FULL_BODY, f"{EXPIRING_THIS_MONTH_HEADING} (")
    assert [line.split()[0] for line in this_month] == ["c-this"]
    assert "$120.50" in this_month[0]
    assert "2026-05-28" in this_month[0]

    next_three = _lines_under(FULL_BODY, f"{EXPIRING_NEXT_THREE_HEADING} (")
    assert [line.split()[0] for line in next_three] == ["c-next-a", "c-next-b"]
    assert "$300.00" in next_three[0] and "2026-07-01" in next_three[0]
    # A missing remainingAmount is rendered as "not available", not dropped.
    assert NOT_AVAILABLE in next_three[1] and "2026-08-31" in next_three[1]

    # Credits outside both tiers never appear in an alert.
    alerts_block = FULL_BODY[: FULL_BODY.index(DASHBOARD_SUMMARY_HEADING)]
    assert "c-far" not in alerts_block
    assert f"{EXPIRING_THIS_MONTH_HEADING} (1 credit, total remaining $120.50):" in FULL_BODY
    assert f"{EXPIRING_NEXT_THREE_HEADING} (2 credits, total remaining $300.00):" in FULL_BODY


def test_no_expiration_alert_when_both_tiers_are_empty():
    body = _body(FAR + NEW, prior_ids=["c-far"])

    alerts_block = body[: body.index(DASHBOARD_SUMMARY_HEADING)]
    assert OVERVIEW_HEADING in alerts_block  # the new-credit alert is still there
    assert f"{NEW_CREDITS_ALERT_HEADING} (" in alerts_block
    assert f"{EXPIRING_THIS_MONTH_HEADING} (" not in alerts_block
    assert f"{EXPIRING_NEXT_THREE_HEADING} (" not in alerts_block
    assert "ends " not in alerts_block.split(f"{NEW_CREDITS_ALERT_HEADING} (")[0]


def test_only_the_title_and_no_alert_blocks_when_nothing_is_new_or_expiring():
    body = _body(FAR, prior_ids=["c-far"])

    # The title is always present; with no alerts it is followed by the summary.
    assert body.startswith(f"{OVERVIEW_HEADING}\n\n{DASHBOARD_SUMMARY_HEADING}")
    assert NEW_CREDITS_ALERT_HEADING not in body
    # The dashboard still reports the (zero) tier figures.
    assert f"- {EXPIRING_THIS_MONTH_HEADING}: 0 credits, total remaining 0.00" in body
    assert f"- {EXPIRING_NEXT_THREE_HEADING}: 0 credits, total remaining 0.00" in body
    assert NO_NEW_CREDITS_MESSAGE in body


def test_dashboard_summary_reports_active_and_tier_figures():
    assert "- Active credits: 5 credits, total remaining $1462.50" in FULL_BODY
    assert f"- {EXPIRING_THIS_MONTH_HEADING}: 1 credit, total remaining $120.50" in FULL_BODY
    assert f"- {EXPIRING_NEXT_THREE_HEADING}: 2 credits, total remaining $300.00" in FULL_BODY
    assert "- Newly issued credits: 1 credit, total remaining $42.00" in FULL_BODY
    assert NO_ACTIVE_CREDITS_MESSAGE not in FULL_BODY
    assert NO_NEW_CREDITS_MESSAGE not in FULL_BODY


def test_empty_states_when_no_active_and_no_new_credits():
    disabled = [_credit("c-off", "5", date(2027, 1, 1), status="DISABLED")]
    body = _body(disabled, prior_ids=["c-off"])

    assert NO_ACTIVE_CREDITS_MESSAGE in body
    assert NO_NEW_CREDITS_MESSAGE in body
    # The only alert is the paused credit.
    assert NEW_CREDITS_ALERT_HEADING not in body
    assert "Paused credits (1 credit, total remaining $5.00):" in body


def test_baseline_and_comparison_error_notices_propagate_to_the_body():
    baseline = _body(CURRENT, prior_ids=None)
    assert BASELINE_NOTICE in baseline
    assert COMPARISON_ERROR_NOTICE not in baseline
    # On a baseline nothing is flagged new, so no new-credit alert.
    assert NEW_CREDITS_ALERT_HEADING not in baseline
    assert NO_NEW_CREDITS_MESSAGE in baseline

    errored = _body(CURRENT, prior_ids=["c-far"], prior_ok=False)
    assert COMPARISON_ERROR_NOTICE in errored
    assert BASELINE_NOTICE not in errored
    assert NEW_CREDITS_ALERT_HEADING not in errored


def test_credit_alert_line_renders_missing_fields_as_not_available():
    line = format_credit_alert_bullet(Credit())
    assert line.count(NOT_AVAILABLE) == 3


def test_published_message_is_the_body_unchanged():
    client = FakeClient()
    result = _notifier(client).send(FULL_BODY)

    assert result.sent is True
    _, kwargs = client.calls[0]
    assert kwargs["Message"] == FULL_BODY
    assert OVERVIEW_HEADING in kwargs["Message"]
    assert DASHBOARD_SUMMARY_HEADING in kwargs["Message"]
    assert "c-this" in kwargs["Message"] and "c-new" in kwargs["Message"]
    assert "ttachment" not in kwargs["Message"]



def test_trailing_listing_is_omitted_when_only_old_expired_credits_exist():
    """Only expired credits older than the window: no trailing section at all."""
    from datetime import date, datetime, timezone
    from decimal import Decimal

    from aws_credits_reporter.dashboard import build_dashboard
    from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
    from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
    from aws_credits_reporter.models import Credit, CurrencyAmount
    from aws_credits_reporter.new_credit_detector import NewCreditResult
    from aws_credits_reporter.report_body import (
        EXPIRED_CREDITS_HEADING,
        INACTIVE_CREDITS_HEADING,
        build_dashboard_body,
    )

    snapshot = date(2026, 10, 22)
    old = Credit(
        creditId="OLD",
        endDate=date(2025, 1, 1),
        creditStatus="ENABLED",
        remainingAmount=CurrencyAmount(Decimal("5"), "USD"),
    )
    lifecycle = LifecycleAnalyzer(snapshot).partition([old])
    expiration = ExpirationAnalyzer(datetime(2026, 10, 22, tzinfo=timezone.utc)).classify(
        lifecycle.active
    )
    new_credits = NewCreditResult()
    dashboard = build_dashboard(lifecycle, expiration, new_credits)

    body = build_dashboard_body(
        dashboard, expiration, new_credits, lifecycle.inactive, snapshot_date=snapshot
    )

    assert EXPIRED_CREDITS_HEADING not in body
    assert INACTIVE_CREDITS_HEADING not in body
    assert "OLD" not in body
    # The body ends with the sharing block: nothing is appended after it.
    assert body.rstrip().splitlines()[-1].startswith("- Sharing disabled:")
