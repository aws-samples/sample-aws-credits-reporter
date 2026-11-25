"""Tests for the run source flag: manual runs do not advance the baseline."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from aws_credits_reporter.lambda_handler import (
    ENV_BUCKET,
    ENV_OUTCOME_TOPIC_ARN,
    LambdaConfig,
    handler,
    run_source_from_event,
)
from aws_credits_reporter.models import (
    SNAPSHOT_SOURCE_MANUAL,
    SNAPSHOT_SOURCE_SCHEDULED,
    Snapshot,
)
from aws_credits_reporter.orchestrator import RunResult
from aws_credits_reporter.snapshot_store import SnapshotStore
from tests.s3_clock import ClockedS3

BUCKET = "amzn-s3-demo-credits-source-tests"
PREFIX = "snapshots/"
T0 = datetime(2025, 6, 1, 8, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=10)
SCHEDULED_EVENT = {"source": "aws.events", "detail-type": "Scheduled Event", "resources": ["arn:x"]}


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


# --------------------------------------------------------------------------
# Event classification
# --------------------------------------------------------------------------


def test_schedule_event_is_scheduled():
    assert run_source_from_event(SCHEDULED_EVENT) == SNAPSHOT_SOURCE_SCHEDULED


@pytest.mark.parametrize(
    "event",
    [
        {},
        None,
        "x",
        [],
        {"source": "aws.events"},
        {"detail-type": "Scheduled Event"},
        {"source": "other", "detail-type": "Scheduled Event"},
    ],
)
def test_anything_else_is_manual(event):
    assert run_source_from_event(event) == SNAPSHOT_SOURCE_MANUAL


class _Orch:
    def run(self):
        return RunResult(success=True, snapshot_key="k")


class _Sns:
    def publish(self, **kwargs):
        return {"MessageId": "1"}


class _Ctx:
    aws_request_id = "r"


def _config_seen(event) -> LambdaConfig:
    seen: list[LambdaConfig] = []

    def factory(config):
        seen.append(config)
        return _Orch()

    handler(
        event,
        _Ctx(),
        orchestrator_factory=factory,
        sns_client=_Sns(),
        env={ENV_BUCKET: "b", ENV_OUTCOME_TOPIC_ARN: "arn:aws:sns:us-east-1:1:t"},
    )
    return seen[0]


def test_handler_passes_the_run_source_to_the_factory():
    assert _config_seen(SCHEDULED_EVENT).run_source == SNAPSHOT_SOURCE_SCHEDULED
    assert _config_seen({}).run_source == SNAPSHOT_SOURCE_MANUAL


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------


def _doc(**extra) -> bytes:
    base = {"taken_at": T0.isoformat(), "get_credits": [], "cost_categories": []}
    base.update(extra)
    return json.dumps(base).encode()


def test_serialize_records_the_source():
    snap = Snapshot(key="", taken_at=T0, source=SNAPSHOT_SOURCE_MANUAL)
    assert json.loads(SnapshotStore.serialize(snap))["source"] == "manual"


def test_snapshot_without_source_is_scheduled():
    assert SnapshotStore.deserialize("k", _doc()).source == SNAPSHOT_SOURCE_SCHEDULED


def test_unknown_source_counts_as_manual():
    assert SnapshotStore.deserialize("k", _doc(source="evil")).source == SNAPSHOT_SOURCE_MANUAL


# --------------------------------------------------------------------------
# Baseline selection
# --------------------------------------------------------------------------


def _write(store: SnapshotStore, taken_at: datetime, source: str) -> str:
    return store.write(Snapshot(key="", taken_at=taken_at, source=source))


def _store(s3) -> SnapshotStore:
    return SnapshotStore(ClockedS3(s3), BUCKET, PREFIX, now=lambda: NOW, sleep=lambda _: None)


def test_manual_snapshot_does_not_replace_the_scheduled_baseline(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = _store(s3)
        scheduled = _write(store, T0, SNAPSHOT_SOURCE_SCHEDULED)
        _write(store, T0 + timedelta(hours=3), SNAPSHOT_SOURCE_MANUAL)
        _write(store, T0 + timedelta(hours=5), SNAPSHOT_SOURCE_MANUAL)
        prior = store.read_latest_prior()
        assert prior is not None and prior.key == scheduled


def test_newest_scheduled_wins_over_newer_manual(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = _store(s3)
        _write(store, T0, SNAPSHOT_SOURCE_SCHEDULED)
        second = _write(store, T0 + timedelta(days=1), SNAPSHOT_SOURCE_SCHEDULED)
        _write(store, T0 + timedelta(days=1, hours=2), SNAPSHOT_SOURCE_MANUAL)
        prior = store.read_latest_prior()
        assert prior is not None and prior.key == second


def test_only_manual_snapshots_fall_back_to_the_newest_manual(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = _store(s3)
        _write(store, T0, SNAPSHOT_SOURCE_MANUAL)
        newest = _write(store, T0 + timedelta(days=1), SNAPSHOT_SOURCE_MANUAL)
        prior = store.read_latest_prior()
        assert prior is not None and prior.key == newest


def test_before_key_still_limits_the_candidates(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = _store(s3)
        first = _write(store, T0, SNAPSHOT_SOURCE_SCHEDULED)
        second = _write(store, T0 + timedelta(days=1), SNAPSHOT_SOURCE_SCHEDULED)
        prior = store.read_latest_prior(before_key=second)
        assert prior is not None and prior.key == first
