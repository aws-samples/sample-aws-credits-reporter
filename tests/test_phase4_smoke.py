"""Phase 4 checkpoint: two consecutive runs against moto S3.

Proves that the orchestrator's detection step works with the real
``SnapshotStore`` read path (``list_objects_v2`` + ``get_object``) rather
than a hand-rolled fake: the second run finds the first run's object as its
prior and flags the added credit. A garbage object under the prefix
exercises the comparison-error path (Req 6.7). The Billing client is a
botocore ``Stubber``; no real AWS account is touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from aws_credits_reporter.credit_retriever import CreditRetriever, make_billing_client
from aws_credits_reporter.new_credit_detector import BASELINE_MESSAGE
from aws_credits_reporter.orchestrator import PRIOR_READ_FAILED_PREFIX, Orchestrator, RunResult
from aws_credits_reporter.snapshot_store import SnapshotStore
from tests.s3_clock import ClockedS3

FIXTURES = Path(__file__).parent / "fixtures"
BUCKET = "amzn-s3-demo-credits-smoke"
PREFIX = "snapshots/"
REGION = "us-east-1"
ACCOUNT = "111111111111"
T0 = datetime(2024, 3, 1, 8, 0, tzinfo=timezone.utc)


def _load(name: str) -> dict:
    page = json.loads((FIXTURES / name).read_text())
    page.pop("nextToken", None)  # not in the current Billing service model
    return page


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _run_once(s3, page: dict, now: datetime) -> RunResult:
    billing = make_billing_client()
    stubber = Stubber(billing)
    stubber.add_response(
        "get_credits",
        page,
        expected_params={"accountId": ACCOUNT, "startDate": "2020-01-01", "payerAccountFlag": True},
    )
    with stubber:
        orchestrator = Orchestrator(
            credit_retriever=CreditRetriever(billing, account_id=ACCOUNT, sleep=lambda _: None),
            snapshot_store=SnapshotStore(
                ClockedS3(s3), bucket=BUCKET, prefix=PREFIX, sleep=lambda _: None
            ),
            now=lambda: now,
        )
        result = orchestrator.run()
        stubber.assert_no_pending_responses()
    return result


def test_two_runs_against_moto_s3_detect_new_credit(aws_env):
    page1 = _load("get_credits_page1.json")
    page2 = _load("get_credits_page2.json")
    union = {"credits": page1["credits"] + page2["credits"]}

    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)

        first = _run_once(s3, page1, T0)
        assert first.success, first.error_message
        assert first.new_credits.is_initial_baseline
        assert BASELINE_MESSAGE in first.messages

        second = _run_once(s3, union, T0 + timedelta(days=1))
        assert second.success, second.error_message
        assert second.prior_snapshot_key == first.snapshot_key
        assert second.new_credits.new_credit_ids == {"credit-0003"}
        assert not second.new_credits.comparison_error
        assert second.dashboard.new_count == 1

        keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX)["Contents"]]
        assert keys == sorted([first.snapshot_key, second.snapshot_key])


def test_garbage_object_under_prefix_is_a_comparison_error_not_fatal(aws_env):
    page1 = _load("get_credits_page1.json")

    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)

        first = _run_once(s3, page1, T0)
        assert first.success
        # An object that lists as a snapshot but is not a valid document,
        # timestamped between the two real runs.
        bad_key = f"{PREFIX}20240302T080000Z.json"
        s3.put_object(Bucket=BUCKET, Key=bad_key, Body=b"not json at all")

        third = _run_once(s3, page1, T0 + timedelta(days=2))
        assert third.success, third.error_message
        # The garbage object is the latest prior, so detection halts (Req 6.7)
        # and the read failure is recorded without failing the run.
        assert third.new_credits.comparison_error
        assert any(m.startswith(PRIOR_READ_FAILED_PREFIX) and bad_key in m for m in third.messages)
