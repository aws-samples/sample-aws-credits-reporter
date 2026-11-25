"""Notifier: SNS delivery of the report body for AWS Credits Reporter.

SNS is the only delivery backend. The body itself is built by ``report_body``.

- ``NotifyResult`` records what happened: whether the message was published,
  how many attempts were made, a configuration error (no topic ARN), a
  delivery failure with its reason, and human-readable ``messages`` for the
  run output.
- ``Notifier.send(body)`` publishes the body to the SNS topic. The publish is
  retried up to ``max_attempts`` (default 3) with exponential backoff under
  an overall ``send_deadline_s`` (default 300 s). After the attempts are
  exhausted, or when the next retry would cross the deadline,
  ``delivery_failed`` is set with the last error as the reason.
- ``dry_run`` prepares the message exactly as for a real send (so a
  configuration error is still detected) but never calls the backend;
  ``NotifyResult.dry_run`` is set and a "dry run: notification not sent"
  message describes what would have gone out. The client may be ``None`` in
  this mode.

The notifier never constructs AWS clients; a fully built client (or a stub)
is injected, as with the retriever and the snapshot store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from botocore.exceptions import ClientError

from aws_credits_reporter.aws_calls import BACKOFF_BASE_SECONDS

DEFAULT_SUBJECT = "AWS Credits Reporter - Credit Status Report"

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_SEND_DEADLINE_SECONDS = 300.0
# SNS caps the Subject attribute at 100 characters.
SNS_SUBJECT_MAX_CHARS = 100


# --------------------------------------------------------------------------
# SNS delivery
# --------------------------------------------------------------------------


@dataclass
class NotifyResult:
    """Outcome of one ``Notifier.send()``.

    ``sent`` is True when the backend accepted the message. ``attempts`` is
    the number of backend calls made (0 when a configuration error stopped
    the send before any call). ``config_error`` holds the message
    when no topic ARN is configured. ``delivery_failed`` with
    ``failure_reason`` is the failure indication after the attempts are
    exhausted. ``dry_run`` is True when the notifier prepared the message but
    deliberately skipped the backend call. ``messages`` collects one
    human-readable line per notable event for ``RunResult``/CLI output.
    """

    sent: bool = False
    attempts: int = 0
    config_error: str | None = None
    delivery_failed: bool = False
    failure_reason: str | None = None
    dry_run: bool = False
    messages: list[str] = field(default_factory=list)


def describe_error(error: BaseException) -> str:
    """``(Code): Message`` for botocore ``ClientError``; ``TypeName: text`` otherwise."""
    if isinstance(error, ClientError) and isinstance(error.response, dict):
        details = error.response.get("Error", {})
        code = str(details.get("Code", "")) if isinstance(details, dict) else ""
        message = str(details.get("Message", "")) if isinstance(details, dict) else ""
        if code or message:
            return f"({code}): {message or error}"
    text = str(error)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


class Notifier:
    """Publish the notification email body to an SNS topic with retry."""

    def __init__(
        self,
        client: Any,
        sns_topic_arn: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        send_deadline_s: float = DEFAULT_SEND_DEADLINE_SECONDS,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        subject: str = DEFAULT_SUBJECT,
        dry_run: bool = False,
    ) -> None:
        if client is None and not dry_run:
            raise ValueError("a backend client is required unless dry_run=True")
        self._client = client
        self._dry_run = bool(dry_run)
        self._sns_topic_arn = sns_topic_arn
        self._max_attempts = max(1, int(max_attempts))
        self._send_deadline_s = float(send_deadline_s)
        self._sleep = sleep
        self._clock = clock
        self._subject = subject

    # -- configuration -----------------------------------------------------

    def _config_error(self) -> str | None:
        if not self._sns_topic_arn:
            return "no SNS topic ARN is configured; the notification was not published"
        return None

    # -- sending -----------------------------------------------------------

    def send(self, body: str) -> NotifyResult:
        """Publish ``body`` to the topic; never raises for backend errors.

        With no topic ARN a configuration error is recorded and the backend is
        not called. Otherwise the publish is retried up to
        ``max_attempts`` times with exponential backoff, stopping early rather
        than sleeping past the overall ``send_deadline_s``; exhaustion records
        ``delivery_failed`` with the last error as the reason.
        """
        result = NotifyResult()

        config_error = self._config_error()
        if config_error:
            result.config_error = config_error
            result.messages.append(f"configuration error: {config_error}")
            return result

        subject = self._subject[:SNS_SUBJECT_MAX_CHARS]
        topic_arn = self._sns_topic_arn

        if self._dry_run:
            result.dry_run = True
            result.messages.append(
                f"dry run: notification not sent; would send to {self._target()}"
            )
            return result

        started = self._clock()
        deadline = started + self._send_deadline_s
        last_error: BaseException | None = None
        deadline_hit = False

        for attempt in range(1, self._max_attempts + 1):
            result.attempts = attempt
            try:
                self._client.publish(TopicArn=topic_arn, Subject=subject, Message=body)
            except Exception as error:  # noqa: BLE001 - every backend failure is recorded, not raised
                last_error = error
            else:
                result.sent = True
                result.messages.append(self._success_message(attempt))
                return result

            if attempt >= self._max_attempts:
                break
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            if self._clock() + delay >= deadline:
                deadline_hit = True
                break
            self._sleep(delay)

        assert last_error is not None
        reason = describe_error(last_error)
        if deadline_hit:
            summary = (
                f"delivery failed after {result.attempts} attempt(s); the "
                f"{self._send_deadline_s:g} s send deadline would be exceeded by another retry"
            )
        else:
            summary = f"delivery failed after {result.attempts} attempt(s)"
        result.delivery_failed = True
        result.failure_reason = f"{summary}: {reason}"
        result.messages.append(f"delivery failure: {result.failure_reason}")
        return result

    def _target(self) -> str:
        return f"SNS topic {self._sns_topic_arn}"

    def _success_message(self, attempt: int) -> str:
        suffix = f" on attempt {attempt}" if attempt > 1 else ""
        return f"notification sent to {self._target()}{suffix}"
