"""Notifier wired into the Orchestrator and the CLI (SNS only).

Uses in-memory fakes for the retriever, the snapshot store, and the SNS
client; no AWS client is constructed anywhere in this module.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from aws_credits_reporter.cli import (
    NOTIFICATION_SKIPPED_MESSAGE,
    build_notifier,
    main,
    parse_args,
)
from aws_credits_reporter.credit_retriever import RetrievalError, RetrievalOutcome
from aws_credits_reporter.models import Snapshot, parse_credits
from aws_credits_reporter.notifier import Notifier, NotifyResult
from aws_credits_reporter.report_body import DASHBOARD_SUMMARY_HEADING, build_dashboard_body
from aws_credits_reporter.orchestrator import Orchestrator

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_KEY = "snapshots/20240101T000000Z.json"
TOPIC_ARN = "arn:aws:sns:us-east-1:111111111111:credits"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeRetriever:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def retrieve(self) -> RetrievalOutcome:
        return RetrievalOutcome(raw_pages=list(self._pages), credits=parse_credits(self._pages))


class FakeStore:
    """Single-write store with no prior snapshot: every run is an initial baseline."""

    def write(self, snapshot: Snapshot) -> str:
        snapshot.key = SNAPSHOT_KEY
        return SNAPSHOT_KEY

    def read_latest_prior(self, before_key: str | None = None) -> Snapshot | None:
        return None


class FakeClient:
    """Records ``publish`` calls; the first ``failures`` calls raise."""

    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._failures = failures

    def _record(self, name: str, kwargs: dict) -> dict:
        self.calls.append((name, kwargs))
        if len(self.calls) <= self._failures:
            raise ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "Publish",
            )
        return {"MessageId": f"m-{len(self.calls)}"}

    def publish(self, **kwargs):
        return self._record("publish", kwargs)


class RecordingNotifier:
    """Captures what the orchestrator hands to ``send`` and returns a canned result."""

    def __init__(self, result: NotifyResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def send(self, body: str) -> NotifyResult:
        self.calls.append(body)
        return self.result


def _fixed_now() -> datetime:
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def _orchestrator(pages: list[dict], notifier=None) -> Orchestrator:
    return Orchestrator(
        credit_retriever=FakeRetriever(pages),
        snapshot_store=FakeStore(),
        now=_fixed_now,
        notifier=notifier,
    )


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


def test_orchestrator_sends_dashboard_summary_body():
    page = _load("get_credits_page1.json")
    notifier = RecordingNotifier(NotifyResult(sent=True, attempts=1, messages=["notification sent"]))

    result = _orchestrator([page], notifier).run()

    assert result.success
    assert len(notifier.calls) == 1
    body = notifier.calls[0]
    # Req 11.1: the body is the Dashboard_Summary.
    assert body == build_dashboard_body(
        result.dashboard,
        result.expiration,
        result.new_credits,
        result.inactive_credits,
    )
    assert DASHBOARD_SUMMARY_HEADING in body
    assert result.notify_result is notifier.result
    assert result.delivery_failed is False
    assert "notification sent" in result.messages


def test_orchestrator_surfaces_delivery_failure_without_failing_run():
    notify = NotifyResult(
        attempts=3,
        delivery_failed=True,
        failure_reason="delivery failed after 3 attempt(s): (Throttling): Rate exceeded",
        messages=["delivery failure: delivery failed after 3 attempt(s): (Throttling): Rate exceeded"],
    )
    result = _orchestrator([_load("get_credits_page1.json")], RecordingNotifier(notify)).run()

    assert result.success  # snapshot written (Req 5.6 is an indication, not a failure)
    assert result.snapshot_key == SNAPSHOT_KEY
    assert result.delivery_failed is True
    assert any(m.startswith("delivery failure:") for m in result.messages)


def test_orchestrator_surfaces_config_error_message():
    notify = NotifyResult(
        config_error="no SNS topic ARN is configured; the notification was not published",
        messages=["configuration error: no SNS topic ARN is configured; the notification was not published"],
    )
    result = _orchestrator([{"credits": []}], RecordingNotifier(notify)).run()

    assert result.success
    assert result.delivery_failed is False
    assert result.messages[-1].startswith("configuration error: no SNS topic ARN")


def test_orchestrator_without_notifier_skips_notification():
    page = _load("get_credits_page1.json")
    baseline = _orchestrator([page]).run()

    assert baseline.success
    assert baseline.notify_result is None
    assert baseline.delivery_failed is False
    assert not any("notification" in m for m in baseline.messages)


def test_orchestrator_does_not_notify_when_run_failed_earlier():
    class FailingRetriever:
        def retrieve(self) -> RetrievalOutcome:
            raise RetrievalError("GetCredits failed")

    notifier = RecordingNotifier(NotifyResult(sent=True))
    result = Orchestrator(
        credit_retriever=FailingRetriever(),
        snapshot_store=FakeStore(),
        now=_fixed_now,
        notifier=notifier,
    ).run()

    assert not result.success
    assert notifier.calls == []
    assert result.notify_result is None


# --------------------------------------------------------------------------
# CLI argument handling
# --------------------------------------------------------------------------


def test_build_notifier_returns_none_without_notification_options():
    args = parse_args(["--bucket", "b"])
    assert build_notifier(args, lambda service: pytest.fail("no client expected")) is None


def test_build_notifier_uses_sns_client_and_topic_arn():
    requested: list[str] = []
    fake = FakeClient()

    def factory(service: str):
        requested.append(service)
        return fake

    args = parse_args(["--bucket", "b", "--sns-topic-arn", TOPIC_ARN])
    notifier = build_notifier(args, factory, sleep=lambda _: None)

    assert isinstance(notifier, Notifier)
    assert requested == ["sns"]
    result = notifier.send("body\n")
    assert result.sent
    assert fake.calls[0][0] == "publish"
    assert fake.calls[0][1]["TopicArn"] == TOPIC_ARN
    assert fake.calls[0][1]["Message"] == "body\n"


def test_build_notifier_dry_run_builds_no_client():
    args = parse_args(["--bucket", "b", "--sns-topic-arn", TOPIC_ARN, "--dry-run"])
    notifier = build_notifier(args, lambda service: pytest.fail("dry run must not build a client"))
    assert isinstance(notifier, Notifier)


@pytest.mark.parametrize(
    "argv",
    [
        ["--output-dir", "out"],
        ["--recipients", "a@x.io"],
        ["--sender", "s@x.io"],
        ["--notify-backend", "sns"],
    ],
)
def test_removed_options_are_usage_errors(argv, capsys):
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--bucket", "b", *argv])
    assert exit_info.value.code == 2
    assert "usage:" in capsys.readouterr().err


# --------------------------------------------------------------------------
# CLI end to end (fake orchestrator components, fake SNS client)
# --------------------------------------------------------------------------


def _cli_factory(page: dict, client: FakeClient):
    def factory(args):
        notifier = build_notifier(args, lambda service: client, sleep=lambda _: None)
        return _orchestrator([page], notifier)

    return factory


def test_cli_sns_topic_publishes_once_and_reports_it(capsys):
    page = _load("get_credits_page1.json")
    client = FakeClient()

    exit_code = main(
        ["--bucket", "b", "--sns-topic-arn", TOPIC_ARN],
        orchestrator_factory=_cli_factory(page, client),
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert [name for name, _ in client.calls] == ["publish"]
    assert client.calls[0][1]["TopicArn"] == TOPIC_ARN
    assert DASHBOARD_SUMMARY_HEADING in client.calls[0][1]["Message"]
    assert f"notification sent to SNS topic {TOPIC_ARN}" in captured.out
    assert NOTIFICATION_SKIPPED_MESSAGE not in captured.out
    assert "ttachment" not in captured.out
    assert captured.err == ""
    # Run output intact.
    assert f"Total credits: {len(page['credits'])}" in captured.out
    assert f"Snapshot written: {SNAPSHOT_KEY}" in captured.out


def test_cli_dry_run_without_topic_prints_config_error(capsys):
    client = FakeClient()

    exit_code = main(
        ["--bucket", "b", "--dry-run"],
        orchestrator_factory=_cli_factory(_load("get_credits_page1.json"), client),
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert client.calls == []
    assert "configuration error: no SNS topic ARN is configured" in captured.out
    assert "notification sent" not in captured.out


def test_cli_delivery_failure_prints_warning_and_exits_zero(capsys):
    client = FakeClient(failures=3)

    exit_code = main(
        ["--bucket", "b", "--sns-topic-arn", TOPIC_ARN],
        orchestrator_factory=_cli_factory(_load("get_credits_page1.json"), client),
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert len(client.calls) == 3
    assert "delivery failure: delivery failed after 3 attempt(s)" in captured.out
    assert "warning: notification delivery failed:" in captured.err
    assert "(Throttling): Rate exceeded" in captured.err


def test_cli_dry_run_makes_no_backend_calls(capsys):
    client = FakeClient()

    exit_code = main(
        ["--bucket", "b", "--sns-topic-arn", TOPIC_ARN, "--dry-run"],
        orchestrator_factory=_cli_factory(_load("get_credits_page1.json"), client),
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert client.calls == []
    assert f"dry run: notification not sent; would send to SNS topic {TOPIC_ARN}" in captured.out
    assert "notification sent" not in captured.out
    assert captured.err == ""


def test_cli_without_notification_options_prints_skipped_line(capsys):
    client = FakeClient()

    exit_code = main(
        ["--bucket", "b"], orchestrator_factory=_cli_factory(_load("get_credits_page1.json"), client)
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert client.calls == []
    assert NOTIFICATION_SKIPPED_MESSAGE in captured.out
