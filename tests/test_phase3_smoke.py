"""Phase 3 checkpoint: end-to-end smoke test for email delivery over SNS.

Exercises the real CLI code path with ``--sns-topic-arn`` (retrieve -> snapshot
write -> dashboard body -> SNS ``publish``) using a botocore ``Stubber`` for
the Billing client and ``moto`` for S3 *and* SNS.

Unlike ``tests/test_notify_wiring.py``, which drives the notifier with a
hand-rolled fake, this module publishes through a real boto3 SNS client so the
request parameters are validated against the SNS service model and the
published message is inspected as moto received it. No real AWS account is
touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

from aws_credits_reporter.cli import build_notifier, main
from aws_credits_reporter.credit_retriever import CreditRetriever, make_billing_client
from aws_credits_reporter.notifier import DEFAULT_SUBJECT
from aws_credits_reporter.report_body import DASHBOARD_SUMMARY_HEADING, NEW_CREDITS_ALERT_HEADING, OVERVIEW_HEADING, SHARING_SUMMARY_HEADING
from aws_credits_reporter.orchestrator import Orchestrator
from aws_credits_reporter.snapshot_store import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"
BUCKET = "amzn-s3-demo-credits-smoke"
PREFIX = "snapshots/"
REGION = "us-east-1"
TOPIC_NAME = "aws-credits-reporter-report"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _run_cli(page: dict, extra_argv: list[str], *, topic_exists: bool = True) -> tuple[int, str, list]:
    """Run ``main`` against a stubbed Billing page, moto S3, and a real boto3 SNS client.

    Returns the exit code, the topic ARN used, and the notifications moto's SNS
    backend accepted for that topic.
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        sns = boto3.client("sns", region_name=REGION)
        if topic_exists:
            topic_arn = sns.create_topic(Name=TOPIC_NAME)["TopicArn"]
        else:
            topic_arn = f"arn:aws:sns:{REGION}:{DEFAULT_ACCOUNT_ID}:{TOPIC_NAME}"

        billing = make_billing_client()
        stubber = Stubber(billing)
        stubber.add_response(
            "get_credits",
            page,
            expected_params={
                "accountId": "111111111111",
                "startDate": "2020-01-01",
                "payerAccountFlag": True,
            },
        )
        stubber.activate()

        def factory(args):
            retriever = CreditRetriever(billing, account_id="111111111111", sleep=lambda _: None)
            store = SnapshotStore(s3, bucket=args.bucket, prefix=args.prefix, sleep=lambda _: None)

            def client_factory(service: str):
                assert service == "sns", f"unexpected client requested: {service}"
                return sns

            notifier = build_notifier(args, client_factory, sleep=lambda _: None)
            return Orchestrator(credit_retriever=retriever, snapshot_store=store, notifier=notifier)

        exit_code = main(
            ["--bucket", BUCKET, "--prefix", PREFIX, "--sns-topic-arn", topic_arn, *extra_argv],
            orchestrator_factory=factory,
        )
        stubber.assert_no_pending_responses()
        topic = sns_backends[DEFAULT_ACCOUNT_ID][REGION].topics.get(topic_arn)
        published = list(topic.sent_notifications) if topic is not None else []
        return exit_code, topic_arn, published


def test_cli_publishes_the_email_body_to_the_sns_topic(aws_env, capsys):
    page = _load("get_credits_page1.json")
    page.pop("nextToken", None)  # not in the current Billing service model

    exit_code, topic_arn, published = _run_cli(page, [])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert f"notification sent to SNS topic {topic_arn}" in captured.out
    assert "ttachment" not in captured.out

    # Exactly one publish, carrying the subject and the plain-text body (Req 5.1).
    assert len(published) == 1
    _message_id, message, subject, _attributes, _group_id = published[0]
    assert subject == DEFAULT_SUBJECT

    # Body carries the Dashboard_Summary, the sharing block, and the trailing
    # listing (Req 11.1), and nothing about attachments.
    assert DASHBOARD_SUMMARY_HEADING in message
    assert "Active credits: " in message
    assert "total remaining" in message
    assert SHARING_SUMMARY_HEADING in message
    assert message.startswith(OVERVIEW_HEADING)
    assert "ttachment" not in message
    # Baseline run: no prior snapshot, so nothing is flagged new.
    assert f"{NEW_CREDITS_ALERT_HEADING} (" not in message


def test_cli_dry_run_publishes_nothing(aws_env, capsys):
    page = _load("get_credits_page1.json")
    page.pop("nextToken", None)

    exit_code, topic_arn, published = _run_cli(page, ["--dry-run"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert published == []
    assert f"dry run: notification not sent; would send to SNS topic {topic_arn}" in captured.out


def test_cli_missing_topic_records_delivery_failure_and_exits_zero(aws_env, capsys):
    """A real SNS rejection (topic not found) surfaces as Req 5.6, not a crash."""
    page = _load("get_credits_page1.json")
    page.pop("nextToken", None)

    exit_code, _topic_arn, published = _run_cli(page, [], topic_exists=False)
    captured = capsys.readouterr()

    assert exit_code == 0  # snapshot written; delivery is not fatal
    assert published == []
    assert "delivery failure: delivery failed after 3 attempt(s): (NotFound)" in captured.out
    assert "warning: notification delivery failed" in captured.err
    assert "NotFound" in captured.err
