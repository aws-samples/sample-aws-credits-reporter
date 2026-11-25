"""Phase 1 checkpoint: end-to-end smoke test with stubbed AWS clients.

Exercises the real CLI code path (retrieve -> snapshot write -> print listing)
using a botocore ``Stubber`` for the Billing client (so request and response
shapes are validated against the real service model) and ``moto`` for S3.
No real AWS account is touched.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from aws_credits_reporter.cli import main
from aws_credits_reporter.credit_retriever import CreditRetriever, make_billing_client
from aws_credits_reporter.orchestrator import Orchestrator
from aws_credits_reporter.snapshot_store import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"
BUCKET = "amzn-s3-demo-credits-smoke"
PREFIX = "snapshots/"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def test_cli_end_to_end_with_stubbed_clients(aws_env, monkeypatch, capsys):
    page = _load("get_credits_page1.json")
    # The current Billing service model exposes no pagination token in the
    # GetCredits output, and Stubber validates responses against that model.
    # Drop the fixture's token so this single-page smoke test stays valid.
    page.pop("nextToken", None)
    expected_ids = [c["creditId"] for c in page["credits"]]

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

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
            return Orchestrator(credit_retriever=retriever, snapshot_store=store)

        exit_code = main(["--bucket", BUCKET, "--prefix", PREFIX], orchestrator_factory=factory)
        stubber.assert_no_pending_responses()

        out = capsys.readouterr().out
        assert exit_code == 0

        # One line per credit (Req 3.4) and the totals (Req 3.5).
        for credit_id in expected_ids:
            assert any(line.startswith(credit_id + " ") for line in out.splitlines()), credit_id
        assert f"Total credits: {len(expected_ids)}" in out
        assert "Total remaining:" in out

        # Snapshot written under the prefix with the unmodified raw page (Req 2.1).
        listing = s3.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX)
        keys = [o["Key"] for o in listing.get("Contents", [])]
        assert len(keys) == 1
        assert f"Snapshot written: {keys[0]}" in out
        body = json.loads(s3.get_object(Bucket=BUCKET, Key=keys[0])["Body"].read())
        assert body["get_credits"] == [page]
        assert body["cost_categories"] == []


def test_cli_zero_credits_prints_zero_message(aws_env, capsys):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        billing = make_billing_client()
        stubber = Stubber(billing)
        stubber.add_response("get_credits", {"credits": []})
        stubber.activate()

        def factory(args):
            retriever = CreditRetriever(billing, account_id="111111111111", sleep=lambda _: None)
            store = SnapshotStore(s3, bucket=args.bucket, prefix=args.prefix, sleep=lambda _: None)
            return Orchestrator(credit_retriever=retriever, snapshot_store=store)

        exit_code = main(["--bucket", BUCKET], orchestrator_factory=factory)
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Zero credits were retrieved." in out
        assert "Total credits: 0" in out


def test_cli_access_denied_exits_nonzero_and_names_permission(aws_env, capsys):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        billing = make_billing_client()
        stubber = Stubber(billing)
        stubber.add_client_error(
            "get_credits",
            service_error_code="AccessDeniedException",
            service_message="not authorized",
            http_status_code=403,
        )
        stubber.activate()

        def factory(args):
            retriever = CreditRetriever(billing, account_id="111111111111", sleep=lambda _: None)
            store = SnapshotStore(s3, bucket=args.bucket, prefix=args.prefix, sleep=lambda _: None)
            return Orchestrator(credit_retriever=retriever, snapshot_store=store)

        exit_code = main(["--bucket", BUCKET], orchestrator_factory=factory)
        captured = capsys.readouterr()
        assert exit_code != 0
        assert "billing:GetCredits" in captured.err
        # Nothing was written to S3 on a failed retrieval.
        assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX)
