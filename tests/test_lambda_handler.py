"""Task 15.1 / 15.3: Lambda handler (Deployment_Runner) without AWS.

Fakes stand in for the orchestrator and the outcome SNS client, so no boto3
client is ever constructed here.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from aws_credits_reporter.lambda_handler import (
    ENV_BUCKET,
    ENV_OUTCOME_TOPIC_ARN,
    ENV_PREFIX,
    ENV_PUBLISH_OUTCOME,
    ENV_SNS_TOPIC_ARN,
    OUTCOME_SUBJECT_FAILURE,
    OUTCOME_SUBJECT_SUCCESS,
    ConfigurationError,
    LambdaConfig,
    RunFailedError,
    handler,
    load_config,
)
from aws_credits_reporter.orchestrator import RunResult

OUTCOME_TOPIC = "arn:aws:sns:us-east-1:111111111111:aws-credits-reporter-outcome"
BASE_ENV = {ENV_BUCKET: "my-bucket", ENV_OUTCOME_TOPIC_ARN: OUTCOME_TOPIC}


class FakeSns:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail = fail

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("sns down")
        return {"MessageId": f"m-{len(self.calls)}"}


class FakeOrchestrator:
    def __init__(self, result: RunResult | None = None, error: BaseException | None = None) -> None:
        self._result = result
        self._error = error
        self.runs = 0

    def run(self) -> RunResult:
        self.runs += 1
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class Context:
    aws_request_id = "req-123"


def _factory(orchestrator: FakeOrchestrator):
    seen: list[LambdaConfig] = []

    def factory(config: LambdaConfig):
        seen.append(config)
        return orchestrator

    factory.seen = seen  # type: ignore[attr-defined]
    return factory


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_load_config_requires_bucket_and_outcome_topic():
    with pytest.raises(ConfigurationError) as info:
        load_config({ENV_BUCKET: "b"})
    assert ENV_OUTCOME_TOPIC_ARN in str(info.value)

    with pytest.raises(ConfigurationError):
        load_config({ENV_OUTCOME_TOPIC_ARN: OUTCOME_TOPIC, ENV_BUCKET: "  "})


def test_load_config_reads_all_variables():
    env = {
        **BASE_ENV,
        ENV_PREFIX: "daily/",
        ENV_SNS_TOPIC_ARN: "arn:aws:sns:us-east-1:111111111111:report",
    }
    config = load_config(env)
    assert config == LambdaConfig(
        bucket="my-bucket",
        outcome_topic_arn=OUTCOME_TOPIC,
        prefix="daily/",
        sns_topic_arn="arn:aws:sns:us-east-1:111111111111:report",
    )
    # SES/attachment options no longer exist.
    for removed in ("recipients", "sender", "notify_backend", "output_dir"):
        assert not hasattr(config, removed)


def test_removed_notification_variables_are_ignored():
    config = load_config(
        {
            **BASE_ENV,
            "AWS_CREDITS_REPORTER_RECIPIENTS": "a@example.com",
            "AWS_CREDITS_REPORTER_SENDER": "bot@example.com",
            "AWS_CREDITS_REPORTER_NOTIFY_BACKEND": "ses",
        }
    )
    assert config == LambdaConfig(bucket="my-bucket", outcome_topic_arn=OUTCOME_TOPIC)
    assert config.sns_topic_arn is None


def test_publish_outcome_flag_defaults_to_true():
    assert load_config(BASE_ENV).publish_success_outcome is True
    # A blank value is treated as unset.
    assert load_config({**BASE_ENV, ENV_PUBLISH_OUTCOME: "  "}).publish_success_outcome is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("TRUE", True), ("false", False), ("False", False), (" false ", False)],
)
def test_publish_outcome_flag_parses_true_and_false(raw, expected):
    config = load_config({**BASE_ENV, ENV_PUBLISH_OUTCOME: raw})
    assert config.publish_success_outcome is expected


@pytest.mark.parametrize("raw", ["0", "no", "yes", "maybe", "off"])
def test_publish_outcome_flag_rejects_other_values(raw):
    with pytest.raises(ConfigurationError) as info:
        load_config({**BASE_ENV, ENV_PUBLISH_OUTCOME: raw})
    assert ENV_PUBLISH_OUTCOME in str(info.value)


def test_handler_fails_fast_on_missing_config_without_touching_clients():
    sns = FakeSns()
    factory = _factory(FakeOrchestrator(RunResult(success=True)))
    with pytest.raises(ConfigurationError):
        handler({}, Context(), orchestrator_factory=factory, sns_client=sns, env={})
    assert sns.calls == []
    assert factory.seen == []


# --------------------------------------------------------------------------
# Success path (Req 10.2)
# --------------------------------------------------------------------------


def test_successful_run_publishes_outcome_message():
    result = RunResult(
        success=True,
        snapshot_key="snapshots/20240101T080000Z.json",
        total_count=2,
        total_remaining=Decimal("1234.50"),
        remaining_currency="USD",
        messages=["credit c-1: missing fields endDate", "notification sent"],
    )
    orchestrator = FakeOrchestrator(result)
    sns = FakeSns()
    env = {**BASE_ENV, ENV_SNS_TOPIC_ARN: "arn:aws:sns:us-east-1:111111111111:report"}

    returned = handler({}, Context(), orchestrator_factory=_factory(orchestrator), sns_client=sns, env=env)

    assert orchestrator.runs == 1
    assert len(sns.calls) == 1
    call = sns.calls[0]
    # Outcome goes to the outcome topic, not the report-notification topic.
    assert call["TopicArn"] == OUTCOME_TOPIC
    assert call["Subject"] == OUTCOME_SUBJECT_SUCCESS
    payload = json.loads(call["Message"])
    assert payload["success"] is True
    assert payload["snapshot_key"] == "snapshots/20240101T080000Z.json"
    assert payload["credit_count"] == 2
    assert payload["total_remaining"] == "1234.50"
    assert payload["remaining_currency"] == "USD"
    assert payload["messages"] == result.messages
    assert payload["request_id"] == "req-123"
    assert payload["error_message"] is None
    assert returned["success"] is True and returned["snapshot_key"] == result.snapshot_key


def test_success_outcome_is_not_published_when_flag_is_false():
    result = RunResult(success=True, snapshot_key="snapshots/20240101T080000Z.json", total_count=1)
    orchestrator = FakeOrchestrator(result)
    sns = FakeSns()
    env = {**BASE_ENV, ENV_PUBLISH_OUTCOME: "false"}

    returned = handler({}, Context(), orchestrator_factory=_factory(orchestrator), sns_client=sns, env=env)

    # The run still happens and returns its outcome; only the SNS message is skipped.
    assert orchestrator.runs == 1
    assert sns.calls == []
    assert returned["success"] is True and returned["snapshot_key"] == result.snapshot_key


def test_success_outcome_is_published_when_flag_is_true():
    sns = FakeSns()
    env = {**BASE_ENV, ENV_PUBLISH_OUTCOME: "true"}
    handler(
        {}, Context(), orchestrator_factory=_factory(FakeOrchestrator(RunResult(success=True))),
        sns_client=sns, env=env,
    )
    assert len(sns.calls) == 1
    assert sns.calls[0]["Subject"] == OUTCOME_SUBJECT_SUCCESS


def test_failure_outcome_is_still_published_when_flag_is_false():
    env = {**BASE_ENV, ENV_PUBLISH_OUTCOME: "false"}

    # Orchestrator raises.
    sns = FakeSns()
    with pytest.raises(ValueError):
        handler(
            {}, Context(), orchestrator_factory=_factory(FakeOrchestrator(error=ValueError("boom"))),
            sns_client=sns, env=env,
        )
    assert len(sns.calls) == 1
    assert sns.calls[0]["TopicArn"] == OUTCOME_TOPIC
    assert sns.calls[0]["Subject"] == OUTCOME_SUBJECT_FAILURE

    # Orchestrator returns success=False.
    sns = FakeSns()
    result = RunResult(success=False, error_message="snapshot write failed")
    with pytest.raises(RunFailedError):
        handler({}, Context(), orchestrator_factory=_factory(FakeOrchestrator(result)), sns_client=sns, env=env)
    assert len(sns.calls) == 1
    assert sns.calls[0]["Subject"] == OUTCOME_SUBJECT_FAILURE


# --------------------------------------------------------------------------
# Failure paths (Req 10.3)
# --------------------------------------------------------------------------


def test_orchestrator_exception_publishes_failure_and_reraises():
    orchestrator = FakeOrchestrator(error=ValueError("boom"))
    sns = FakeSns()

    with pytest.raises(ValueError, match="boom"):
        handler({}, Context(), orchestrator_factory=_factory(orchestrator), sns_client=sns, env=BASE_ENV)

    assert orchestrator.runs == 1
    assert len(sns.calls) == 1
    call = sns.calls[0]
    assert call["TopicArn"] == OUTCOME_TOPIC
    assert call["Subject"] == OUTCOME_SUBJECT_FAILURE
    payload = json.loads(call["Message"])
    assert payload["success"] is False
    # The exception TYPE is published, but the raw exception detail is NOT:
    # outcome_from_exception emits a fixed generic message and keeps str(error)
    # in CloudWatch Logs only.
    assert payload["error_type"] == "ValueError"
    assert "boom" not in payload["error_message"]
    assert "CloudWatch Logs" in payload["error_message"]
    assert payload["request_id"] == "req-123"


def test_unsuccessful_run_result_publishes_failure_and_raises():
    result = RunResult(
        success=False,
        error_message="snapshot write failed after 3 attempts",
        total_count=3,
        messages=["credit c-1: missing fields endDate"],
    )
    sns = FakeSns()

    with pytest.raises(RunFailedError, match="snapshot write failed"):
        handler({}, Context(), orchestrator_factory=_factory(FakeOrchestrator(result)), sns_client=sns, env=BASE_ENV)

    assert len(sns.calls) == 1
    assert sns.calls[0]["Subject"] == OUTCOME_SUBJECT_FAILURE
    payload = json.loads(sns.calls[0]["Message"])
    assert payload["success"] is False
    assert payload["error_message"] == "snapshot write failed after 3 attempts"
    assert payload["credit_count"] == 3
    assert payload["messages"] == result.messages


def test_publish_failure_on_failure_path_does_not_mask_original_error():
    orchestrator = FakeOrchestrator(error=KeyError("root cause"))
    sns = FakeSns(fail=True)

    with pytest.raises(KeyError):
        handler({}, Context(), orchestrator_factory=_factory(orchestrator), sns_client=sns, env=BASE_ENV)
    assert len(sns.calls) == 1


def test_outcome_publish_failure_on_success_path_does_not_fail_invocation():
    """A failed outcome publish after a good run must not trigger a Lambda retry."""
    result = RunResult(success=True, snapshot_key="snapshots/20240101T080000Z.json", total_count=1)
    orchestrator = FakeOrchestrator(result)
    sns = FakeSns(fail=True)

    returned = handler(
        {}, Context(), orchestrator_factory=_factory(orchestrator), sns_client=sns, env=BASE_ENV
    )

    assert orchestrator.runs == 1
    assert len(sns.calls) == 1
    assert returned["success"] is True
