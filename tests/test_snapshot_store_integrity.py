"""Tests for ExpectedBucketOwner and baseline validation."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from aws_credits_reporter.models import Snapshot
from aws_credits_reporter.snapshot_store import SnapshotStore
from tests.s3_clock import ClockedS3

BUCKET = "amzn-s3-demo-credits-integrity-tests"
PREFIX = "snapshots/"
OWNER = "111111111111"
T0 = datetime(2025, 6, 1, 8, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=1)


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


class RecordingS3:
    """Records the keyword arguments of every S3 call and returns canned data."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        return {}

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        return {"Contents": [{"Key": "snapshots/20250601T080000Z.json"}], "IsTruncated": False}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        body = json.dumps({"taken_at": T0.isoformat(), "get_credits": [], "cost_categories": []})
        return {"Body": io.BytesIO(body.encode()), "LastModified": T0 + timedelta(seconds=5)}


def test_expected_bucket_owner_is_sent_on_every_call():
    s3 = RecordingS3()
    store = SnapshotStore(s3, BUCKET, PREFIX, expected_bucket_owner=OWNER, now=lambda: NOW)
    store.write(Snapshot(key="", taken_at=T0, raw_get_credits=[]))
    assert store.read_latest_prior() is not None
    assert [name for name, _ in s3.calls] == ["put_object", "list_objects_v2", "get_object"]
    assert all(kwargs["ExpectedBucketOwner"] == OWNER for _, kwargs in s3.calls)


def test_no_owner_means_no_parameter():
    s3 = RecordingS3()
    store = SnapshotStore(s3, BUCKET, PREFIX, now=lambda: NOW)
    store.read_latest_prior()
    assert all("ExpectedBucketOwner" not in kwargs for _, kwargs in s3.calls)


def _put(s3, key: str, taken_at: datetime, doc_taken_at: datetime | None = None) -> None:
    doc = {
        "taken_at": (doc_taken_at or taken_at).isoformat(),
        "get_credits": [{"credits": []}],
        "cost_categories": [],
    }
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(doc).encode())


def test_valid_snapshot_is_used_and_nothing_rejected(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = SnapshotStore(ClockedS3(s3), BUCKET, PREFIX, now=lambda: NOW)
        _put(s3, "snapshots/20250601T080000Z.json", T0)
        prior = store.read_latest_prior()
        assert prior is not None and prior.key.endswith("20250601T080000Z.json")
        assert store.rejected == []


def test_future_dated_snapshot_is_skipped(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = SnapshotStore(ClockedS3(s3), BUCKET, PREFIX, now=lambda: NOW)
        _put(s3, "snapshots/20250601T080000Z.json", T0)
        future = NOW + timedelta(days=30)
        _put(s3, f"snapshots/{future:%Y%m%dT%H%M%SZ}.json", future)
        prior = store.read_latest_prior()
        assert prior is not None and prior.key.endswith("20250601T080000Z.json")
        assert len(store.rejected) == 1 and "future" in store.rejected[0][1]


def test_key_that_disagrees_with_taken_at_is_skipped(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = SnapshotStore(ClockedS3(s3), BUCKET, PREFIX, now=lambda: NOW)
        _put(s3, "snapshots/20250601T080000Z.json", T0)
        # Planted under a later-looking key but carrying an older document.
        _put(s3, "snapshots/20250601T200000Z.json", T0 + timedelta(hours=12), doc_taken_at=T0)
        prior = store.read_latest_prior()
        assert prior is not None and prior.key.endswith("20250601T080000Z.json")
        assert [r[0] for r in store.rejected] == ["snapshots/20250601T200000Z.json"]
        assert "does not match" in store.rejected[0][1]


def test_non_timestamp_key_is_skipped(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = SnapshotStore(ClockedS3(s3), BUCKET, PREFIX, now=lambda: NOW)
        _put(s3, "snapshots/20250601T080000Z.json", T0)
        _put(s3, "snapshots/zzz-planted.json", T0)
        prior = store.read_latest_prior()
        assert prior is not None and prior.key.endswith("20250601T080000Z.json")
        assert "not a snapshot timestamp" in store.rejected[0][1]


def test_object_written_long_after_its_taken_at_is_skipped(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        # Real moto LastModified is "now" (2020s), far after a 2025-06 taken_at
        # only when the test clock is not applied; here no ClockedS3 is used.
        _put(s3, "snapshots/20200101T080000Z.json", datetime(2020, 1, 1, 8, tzinfo=timezone.utc))
        store = SnapshotStore(s3, BUCKET, PREFIX, now=lambda: datetime.now(timezone.utc))
        assert store.read_latest_prior() is None
        assert "long after" in store.rejected[0][1]


def test_all_candidates_rejected_gives_no_baseline(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = SnapshotStore(ClockedS3(s3), BUCKET, PREFIX, now=lambda: NOW)
        _put(s3, "snapshots/not-a-stamp.json", T0)
        assert store.read_latest_prior() is None
        assert len(store.rejected) == 1
