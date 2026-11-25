"""Deployment_Runner: AWS Lambda entry point for the scheduled path.



- ``load_config(env)`` reads the run configuration from environment
  variables (see ``ENV_*`` below). ``AWS_CREDITS_REPORTER_BUCKET`` and
  ``AWS_CREDITS_REPORTER_OUTCOME_TOPIC_ARN`` are required; everything else is
  optional and mirrors the CLI options. A missing required
  variable raises ``ConfigurationError`` *before* any AWS client is built.
- ``build_orchestrator(config)`` delegates to ``wiring.build_orchestrator`` so
  the CLI and the Lambda share one wiring of real boto3 clients (STS account
  lookup, Billing pinned to us-east-1, S3, and the optional SNS notifier).
- ``handler(event, context)`` is the Lambda entry point. It invokes
  ``Orchestrator.run()`` exactly once (EventBridge triggers it once per
  calendar day) and publishes a run-outcome message to the
  *outcome* SNS topic on completion. A failed outcome publish is logged and
  never changes the result: the report and snapshot already exist, and failing
  the invocation would make Lambda retry the whole run. The outcome topic is
  distinct from the optional report-notification topic the Notifier uses:
  the former tells operators whether the scheduled run worked, the latter
  carries the report to the FinOps team.
- Failure handling: when ``run()`` raises, or returns a
  ``RunResult`` with ``success=False``, a failure message is published to the
  outcome topic and an exception is raised so Lambda marks the invocation as
  failed. That is what lets the EventBridge retry policy (capped at 3
  attempts in the infrastructure template) and the Lambda error
  metrics see the failure. A ``RunResult`` failure raises ``RunFailedError``
  carrying the recorded error message; any other exception is re-raised
  unchanged. If publishing the failure message itself fails, the publish
  error is logged and the *original* failure is still raised, so the root
  cause is never masked by a secondary SNS problem.

Testability: ``handler`` accepts an injected ``orchestrator_factory``, an
injected ``sns_client`` for the outcome topic, and an ``env`` mapping, so it
can be exercised end to end without AWS credentials. Lambda itself only
passes ``(event, context)`` and the defaults build real clients.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

import boto3

from aws_credits_reporter import wiring
from aws_credits_reporter.models import SNAPSHOT_SOURCE_MANUAL, SNAPSHOT_SOURCE_SCHEDULED
from aws_credits_reporter.notifier import SNS_SUBJECT_MAX_CHARS
from aws_credits_reporter.orchestrator import RunResult

logger = logging.getLogger(__name__)

# Environment variable names (all prefixed so they do not collide with the
# Lambda runtime's own variables).
ENV_BUCKET = "AWS_CREDITS_REPORTER_BUCKET"
ENV_PREFIX = "AWS_CREDITS_REPORTER_PREFIX"
ENV_REGION = "AWS_CREDITS_REPORTER_REGION"
ENV_SNS_TOPIC_ARN = "AWS_CREDITS_REPORTER_SNS_TOPIC_ARN"
ENV_OUTCOME_TOPIC_ARN = "AWS_CREDITS_REPORTER_OUTCOME_TOPIC_ARN"
# "true" (default) publishes a run-outcome message on every run. "false"
# silences the success message only; failure messages are always published.
ENV_PUBLISH_OUTCOME = "AWS_CREDITS_REPORTER_PUBLISH_OUTCOME"
ENV_EXPECTED_BUCKET_OWNER = "AWS_CREDITS_REPORTER_EXPECTED_BUCKET_OWNER"

REQUIRED_ENV = (ENV_BUCKET, ENV_OUTCOME_TOPIC_ARN)

OUTCOME_SUBJECT_SUCCESS = "AWS Credits Reporter: scheduled run succeeded"
OUTCOME_SUBJECT_FAILURE = "AWS Credits Reporter: scheduled run FAILED"


class ConfigurationError(Exception):
    """A required environment variable is missing or a value is invalid."""


class RunFailedError(Exception):
    """``Orchestrator.run()`` returned ``success=False``; carries the recorded message."""


class _OrchestratorLike(Protocol):
    def run(self) -> RunResult: ...


class _SnsClientLike(Protocol):
    def publish(self, **kwargs: Any) -> Any: ...


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LambdaConfig:
    """Run configuration read from the Lambda environment.

    ``sns_topic_arn`` is the report-delivery topic; the Notifier is only
    constructed when it is set, matching the CLI's opt-in rule.

    ``publish_success_outcome`` controls the run-outcome message of a
    successful run only. It defaults to True. When False the success message is
    not published, but failure messages still are, so a broken scheduled run is
    never silent.
    """

    bucket: str
    outcome_topic_arn: str
    prefix: str = wiring.DEFAULT_PREFIX
    region: str | None = None
    sns_topic_arn: str | None = None
    publish_success_outcome: bool = True
    expected_bucket_owner: str | None = None
    run_source: str = SNAPSHOT_SOURCE_MANUAL


def _parse_bool(name: str, value: str | None, default: bool) -> bool:
    """Parse a ``true`` / ``false`` variable; blank or unset gives ``default``."""
    cleaned = _clean(value)
    if cleaned is None:
        return default
    lowered = cleaned.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise ConfigurationError(f"{name} must be 'true' or 'false', got {cleaned!r}")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def load_config(env: Mapping[str, str]) -> LambdaConfig:
    """Build a ``LambdaConfig`` from ``env``; raise ``ConfigurationError`` when incomplete."""
    missing = [name for name in REQUIRED_ENV if not _clean(env.get(name))]
    if missing:
        raise ConfigurationError(
            f"missing required environment variable(s): {', '.join(missing)}"
        )
    return LambdaConfig(
        bucket=_clean(env[ENV_BUCKET]),  # type: ignore[arg-type]  # validated above
        outcome_topic_arn=_clean(env[ENV_OUTCOME_TOPIC_ARN]),  # type: ignore[arg-type]
        prefix=_clean(env.get(ENV_PREFIX)) or wiring.DEFAULT_PREFIX,
        region=_clean(env.get(ENV_REGION)),
        sns_topic_arn=_clean(env.get(ENV_SNS_TOPIC_ARN)),
        publish_success_outcome=_parse_bool(
            ENV_PUBLISH_OUTCOME, env.get(ENV_PUBLISH_OUTCOME), default=True
        ),
        expected_bucket_owner=_clean(env.get(ENV_EXPECTED_BUCKET_OWNER)),
    )


def build_orchestrator(config: LambdaConfig) -> _OrchestratorLike:
    """Real wiring (STS, Billing, S3, and the optional SNS notifier)."""
    return wiring.build_orchestrator(
        bucket=config.bucket,
        prefix=config.prefix,
        region=config.region,
        sns_topic_arn=config.sns_topic_arn,
        expected_bucket_owner=config.expected_bucket_owner,
        run_source=config.run_source,
    )


def run_source_from_event(event: Any) -> str:
    """``scheduled`` for the EventBridge schedule's event, otherwise ``manual``.

    A console test, ``aws lambda invoke`` with ``{}`` or any
    other payload is manual and does not advance the baseline. This guards
    against accidental advances; a caller who can invoke the function can still
    forge the schedule's payload.
    """
    if (
        isinstance(event, Mapping)
        and event.get("source") == "aws.events"
        and event.get("detail-type") == "Scheduled Event"
    ):
        return SNAPSHOT_SOURCE_SCHEDULED
    return SNAPSHOT_SOURCE_MANUAL


def build_sns_client(config: LambdaConfig) -> Any:
    return boto3.Session(region_name=config.region).client("sns", region_name=config.region)


# --------------------------------------------------------------------------
# Outcome messages
# --------------------------------------------------------------------------


@dataclass
class RunOutcome:
    """Payload published to the outcome topic; serialized as JSON in the SNS message."""

    success: bool
    finished_at: str
    snapshot_key: str | None = None
    credit_count: int = 0
    total_remaining: str = "0"
    remaining_currency: str | None = None
    delivery_failed: bool = False
    error_message: str | None = None
    error_type: str | None = None
    messages: list[str] = field(default_factory=list)
    request_id: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def _request_id(context: Any) -> str | None:
    return getattr(context, "aws_request_id", None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def outcome_from_result(result: RunResult, context: Any = None) -> RunOutcome:
    """Describe a completed ``RunResult`` (successful or not) for the outcome topic."""
    return RunOutcome(
        success=bool(result.success),
        finished_at=_now_iso(),
        snapshot_key=result.snapshot_key,
        credit_count=result.total_count,
        total_remaining=str(result.total_remaining),
        remaining_currency=result.remaining_currency,
        delivery_failed=bool(result.delivery_failed),
        error_message=None if result.success else (result.error_message or "run failed without a recorded error message"),
        error_type=None if result.success else RunFailedError.__name__,
        messages=list(result.messages),
        request_id=_request_id(context),
    )


def outcome_from_exception(error: BaseException, context: Any = None) -> RunOutcome:
    """Describe an exception raised by the orchestrator for the outcome topic.

    The raw exception text is deliberately NOT published to the outcome topic:
    for a non-AWS exception ``str(error)`` can carry internal implementation
    detail, and the topic may have a broader subscriber list than the operator.
    We publish only the exception *type*, a fixed generic message, and the
    Lambda request id; the full ``str(error)`` is written to CloudWatch Logs by
    the handler (via ``logger.exception``) where it belongs for debugging.
    """
    return RunOutcome(
        success=False,
        finished_at=_now_iso(),
        error_message=(
            "The scheduled run failed with an unexpected error. "
            "See the CloudWatch Logs for this function (request id above) for details."
        ),
        error_type=type(error).__name__,
        request_id=_request_id(context),
    )


def publish_outcome(sns_client: _SnsClientLike, topic_arn: str, outcome: RunOutcome) -> Any:
    subject = OUTCOME_SUBJECT_SUCCESS if outcome.success else OUTCOME_SUBJECT_FAILURE
    return sns_client.publish(
        TopicArn=topic_arn,
        Subject=subject[:SNS_SUBJECT_MAX_CHARS],
        Message=outcome.to_json(),
    )


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------


def handler(
    event: Any,
    context: Any,
    orchestrator_factory: Callable[[LambdaConfig], _OrchestratorLike] | None = None,
    sns_client: _SnsClientLike | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Lambda entry point: run once, publish the outcome, fail the invocation on error.

    Returns the outcome as a plain dict on success (visible in the Lambda
    console / invocation response). Raises on any failure after publishing
    the failure message.
    """
    if env is None:
        env = os.environ
    config = replace(load_config(env), run_source=run_source_from_event(event))
    sns = sns_client if sns_client is not None else build_sns_client(config)
    factory = orchestrator_factory if orchestrator_factory is not None else build_orchestrator

    try:
        orchestrator = factory(config)
        result = orchestrator.run()
    except Exception as error:
        logger.exception("AWS Credits Reporter run raised")
        _publish_quietly(sns, config.outcome_topic_arn, outcome_from_exception(error, context))
        raise

    outcome = outcome_from_result(result, context)
    if not result.success:
        logger.error("AWS Credits Reporter run failed: %s", outcome.error_message)
        _publish_quietly(sns, config.outcome_topic_arn, outcome)
        raise RunFailedError(outcome.error_message)

    # The report and snapshot already exist. A failed outcome publish must not
    # fail the invocation: Lambda/EventBridge would retry the whole run, sending
    # a duplicate report whose new-credit diff sees the snapshot just written.
    if config.publish_success_outcome:
        _publish_quietly(sns, config.outcome_topic_arn, outcome)
    else:
        logger.info("success outcome message skipped (%s=false)", ENV_PUBLISH_OUTCOME)
    logger.info(
        "AWS Credits Reporter run succeeded: %s credits, snapshot %s",
        outcome.credit_count,
        outcome.snapshot_key,
    )
    return asdict(outcome)


def _publish_quietly(sns: _SnsClientLike, topic_arn: str, outcome: RunOutcome) -> None:
    """Publish an outcome; a publish error is logged and never changes the run's result."""
    try:
        publish_outcome(sns, topic_arn, outcome)
    except Exception:  # noqa: BLE001 - logged; the caller keeps the real result
        logger.exception("could not publish the run outcome to %s", topic_arn)
