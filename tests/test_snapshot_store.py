"""Unit tests for Snapshot_Store with mocked S3.

The happy-path write, collision, and empty-bucket read cases run against
``moto``'s in-memory S3 so real bucket semantics (conditional put rejecting an
existing key, no-overwrite behaviour) are exercised. The persistent-write-failure
case uses a botocore ``Stubber`` on a real S3 client, because ``moto`` always
accepts a well-formed ``put_object`` and cannot simulate a call that fails on
every one of the three attempts. No real AWS account is touched.

Covers Requirements 2.1, 2.3, 2.5, 2.6.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from aws_credits_reporter.models import Snapshot
from aws_credits_reporter.snapshot_store import SnapshotStore, SnapshotWriteError

from tests.s3_clock import ClockedS3

BUCKET = "amzn-s3-demo-credits-tests"
PREFIX = "snapshots/"
TAKEN_AT = datetime(2025, 6, 1, 12, 30, 45, tzinfo=timezone.utc)
EXPECTED_KEY = "snapshots/20250601T123045Z.json"

# A representative raw get-credits page, kept unmodified through the round trip.
RAW_GET_CREDITS = [
    {
        "credits": [
            {"creditId": "CR-0001", "remainingAmount": {"amount": "100.00", "currency": "USD"}},
            {"creditId": "CR-0002", "remainingAmount": {"amount": "0.00", "currency": "USD"}},
        ],
        "nextToken": "page-2",
    },
    {"credits": [{"creditId": "CR-0003"}]},
]


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _snapshot() -> Snapshot:
    return Snapshot(key="", taken_at=TAKEN_AT, raw_get_credits=RAW_GET_CREDITS)


def _store(s3_client) -> SnapshotStore:
    # sleep is a no-op so retry backoff does not slow the tests down.
    return SnapshotStore(s3_client, bucket=BUCKET, prefix=PREFIX, sleep=lambda _: None)


# --------------------------------------------------------------------------
# Requirement 2.1: write stores the unmodified raw get-credits JSON under the
# expected timestamp key.
# --------------------------------------------------------------------------


def test_write_stores_unmodified_raw_json_under_expected_key(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = _store(s3)
        snapshot = _snapshot()

        key = store.write(snapshot)

        assert key == EXPECTED_KEY
        assert snapshot.key == EXPECTED_KEY

        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        document = json.loads(body.decode("utf-8"))
        # The raw pages are persisted exactly as handed in.
        assert document["get_credits"] == RAW_GET_CREDITS
        assert document["cost_categories"] == []
        assert document["taken_at"] == TAKEN_AT.isoformat()

        # And the round trip through deserialize reconstructs the snapshot.
        restored = store.read(key)
        assert restored.raw_get_credits == RAW_GET_CREDITS
        assert restored.taken_at == TAKEN_AT


# --------------------------------------------------------------------------
# Requirement 2.3: a name collision writes under the "-1" suffix and leaves the
# original object intact (never overwrites).
# --------------------------------------------------------------------------


def test_collision_writes_dash_one_suffix_and_leaves_original_intact(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = _store(s3)

        first = _snapshot()
        first_key = store.write(first)
        assert first_key == EXPECTED_KEY

        original_bytes = s3.get_object(Bucket=BUCKET, Key=first_key)["Body"].read()

        # A second snapshot at the same timestamp must not clobber the first.
        second = Snapshot(
            key="",
            taken_at=TAKEN_AT,
            raw_get_credits=[{"credits": [{"creditId": "CR-9999"}]}],
        )
        second_key = store.write(second)

        assert second_key == "snapshots/20250601T123045Z-1.json"
        assert second_key != first_key

        # Original is byte-for-byte unchanged.
        assert s3.get_object(Bucket=BUCKET, Key=first_key)["Body"].read() == original_bytes
        # The suffixed object holds the second snapshot's data.
        second_doc = json.loads(
            s3.get_object(Bucket=BUCKET, Key=second_key)["Body"].read().decode("utf-8")
        )
        assert second_doc["get_credits"] == [{"credits": [{"creditId": "CR-9999"}]}]


def test_third_collision_writes_dash_two_suffix(aws_env):
    """Consecutive collisions advance the suffix -1, -2, ... without overwrite."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = _store(s3)

        keys = [store.write(_snapshot()) for _ in range(3)]

        assert keys == [
            EXPECTED_KEY,
            "snapshots/20250601T123045Z-1.json",
            "snapshots/20250601T123045Z-2.json",
        ]
        assert len(set(keys)) == 3


# --------------------------------------------------------------------------
# Requirement 2.6: a write that fails on every one of the 3 attempts raises
# SnapshotWriteError.
# --------------------------------------------------------------------------


def test_write_failing_three_times_raises_snapshot_write_error(aws_env):
    s3 = boto3.client("s3", region_name="us-east-1")
    stubber = Stubber(s3)
    # put_object fails on all three attempts with a retryable server error.
    for _ in range(3):
        stubber.add_client_error(
            "put_object",
            service_error_code="InternalError",
            service_message="We encountered an internal error.",
            http_status_code=500,
        )
    stubber.activate()

    store = _store(s3)
    with pytest.raises(SnapshotWriteError) as excinfo:
        store.write(_snapshot())

    stubber.assert_no_pending_responses()
    error = excinfo.value
    assert error.key == EXPECTED_KEY
    assert error.attempts == 3
    assert "InternalError" in error.last_error


# --------------------------------------------------------------------------
# Requirement 2.5: read_latest_prior on an empty bucket returns None so a first
# run has no baseline and continues.
# --------------------------------------------------------------------------


def test_read_latest_prior_on_empty_bucket_returns_none(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = _store(s3)

        assert store.read_latest_prior() is None
        assert store.read_latest_prior(before_key=EXPECTED_KEY) is None


def test_read_latest_prior_returns_most_recent_below_before_key(aws_env):
    """A non-empty bucket returns the newest snapshot strictly before the cutoff."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = _store(ClockedS3(s3))

        older = Snapshot(
            key="",
            taken_at=datetime(2025, 5, 1, 9, 0, 0, tzinfo=timezone.utc),
            raw_get_credits=[{"credits": [{"creditId": "OLD"}]}],
        )
        newer = Snapshot(
            key="",
            taken_at=datetime(2025, 5, 15, 9, 0, 0, tzinfo=timezone.utc),
            raw_get_credits=[{"credits": [{"creditId": "NEW"}]}],
        )
        older_key = store.write(older)
        store.write(newer)

        # No cutoff -> latest overall.
        latest = store.read_latest_prior()
        assert latest is not None
        assert latest.raw_get_credits == [{"credits": [{"creditId": "NEW"}]}]

        # Cutoff at the newer key -> the older snapshot is the latest prior.
        prior = store.read_latest_prior(before_key=store.make_key(newer.taken_at))
        assert prior is not None
        assert prior.raw_get_credits == [{"credits": [{"creditId": "OLD"}]}]

        # Cutoff at the oldest key -> nothing strictly before it.
        assert store.read_latest_prior(before_key=older_key) is None


# --------------------------------------------------------------------------
# Requirement 13.7: the Phase 5 snapshot now carries the unmodified raw
# list-cost-category-definitions pages alongside get-credits, and the store
# still reads Phase 1-4 snapshots that predate the cost_categories section.
# --------------------------------------------------------------------------

# A representative raw list-cost-category-definitions page, kept unmodified.
RAW_COST_CATEGORIES = [
    {
        "CostCategoryReferences": [
            {
                "CostCategoryArn": "arn:aws:ce::111111111111:costcategory/cc-1",
                "Name": "CreditSharingGroups",
                "SupportedResourceTypes": ["billing:creditsharing"],
            }
        ],
        "NextToken": "page-2",
    },
    {"CostCategoryReferences": [{"CostCategoryArn": "arn:aws:ce::111111111111:costcategory/cc-2"}]},
]


def _snapshot_with_categories() -> Snapshot:
    return Snapshot(
        key="",
        taken_at=TAKEN_AT,
        raw_get_credits=RAW_GET_CREDITS,
        raw_cost_categories=RAW_COST_CATEGORIES,
    )


def test_write_stores_raw_cost_categories_and_round_trips(aws_env):
    """raw_cost_categories is persisted unmodified and reconstructed on read."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = _store(s3)
        snapshot = _snapshot_with_categories()

        key = store.write(snapshot)

        document = json.loads(
            s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
        )
        # Both raw sections are stored exactly as handed in.
        assert document["get_credits"] == RAW_GET_CREDITS
        assert document["cost_categories"] == RAW_COST_CATEGORIES

        restored = store.read(key)
        assert restored.raw_get_credits == RAW_GET_CREDITS
        assert restored.raw_cost_categories == RAW_COST_CATEGORIES


def test_serialize_round_trips_cost_categories_without_s3():
    """serialize/deserialize alone preserve raw_cost_categories (no S3 needed)."""
    snapshot = _snapshot_with_categories()

    restored = SnapshotStore.deserialize(EXPECTED_KEY, SnapshotStore.serialize(snapshot))

    assert restored.key == EXPECTED_KEY
    assert restored.raw_get_credits == RAW_GET_CREDITS
    assert restored.raw_cost_categories == RAW_COST_CATEGORIES
    assert restored.taken_at == TAKEN_AT


def test_deserialize_phase1_4_snapshot_without_cost_categories_section():
    """A snapshot written before Phase 5 has no cost_categories key; it still reads."""
    legacy_document = {
        "taken_at": TAKEN_AT.isoformat(),
        "get_credits": RAW_GET_CREDITS,
        # No "cost_categories" key at all, as Phase 1-4 wrote it.
    }
    data = json.dumps(legacy_document).encode("utf-8")

    restored = SnapshotStore.deserialize(EXPECTED_KEY, data)

    assert restored.raw_get_credits == RAW_GET_CREDITS
    # The absent section deserializes to an empty list, never an error.
    assert restored.raw_cost_categories == []
    assert restored.taken_at == TAKEN_AT


def test_read_phase1_4_object_without_cost_categories_from_s3(aws_env):
    """An older object physically lacking the section reads back with an empty list."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = _store(s3)

        legacy_body = json.dumps(
            {"taken_at": TAKEN_AT.isoformat(), "get_credits": RAW_GET_CREDITS}
        ).encode("utf-8")
        s3.put_object(Bucket=BUCKET, Key=EXPECTED_KEY, Body=legacy_body)

        restored = store.read(EXPECTED_KEY)

        assert restored.raw_get_credits == RAW_GET_CREDITS
        assert restored.raw_cost_categories == []


def test_read_latest_prior_prefers_suffixed_collision_key(aws_env):
    """``Z-1.json`` was written after ``Z.json``; it must win as the latest prior.

    Plain string order puts ``-`` before ``.``, which would pick the older object.
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = _store(ClockedS3(s3))

        store.write(_snapshot())
        second = Snapshot(
            key="",
            taken_at=TAKEN_AT,
            raw_get_credits=[{"credits": [{"creditId": "CR-9999"}]}],
        )
        second_key = store.write(second)
        assert second_key.endswith("Z-1.json")

        prior = store.read_latest_prior()
        assert prior is not None and prior.key == second_key

        # Strictly-before filtering uses the same chronological order.
        before_second = store.read_latest_prior(before_key=second_key)
        assert before_second is not None and before_second.key == EXPECTED_KEY
        assert store.read_latest_prior(before_key=EXPECTED_KEY) is None
