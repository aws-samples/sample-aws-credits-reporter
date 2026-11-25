"""Property test for the SNS publish and retry contract of the Notifier.

For any email body, any number of leading publish failures, and any attempt
limit, the Notifier publishes exactly the body it was given (nothing appended),
makes ``min(failures + 1, max_attempts)`` calls, and reports a delivery
failure if and only if every attempt failed (Requirements 5.1, 5.6).

Exercised against the in-memory ``FakeClient`` from ``tests.test_notifier``;
no AWS client is constructed.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from aws_credits_reporter.notifier import Notifier

from tests.test_notifier import FakeClient, FakeClock

_bodies = st.text(max_size=500)
_failures = st.integers(min_value=0, max_value=6)
_attempt_limits = st.integers(min_value=1, max_value=5)


@settings(max_examples=200)
@given(body=_bodies, failures=_failures, max_attempts=_attempt_limits)
def test_publish_sends_body_unchanged_and_fails_iff_all_attempts_fail(
    body: str, failures: int, max_attempts: int
) -> None:
    client = FakeClient(failures=failures)
    clock = FakeClock()
    result = Notifier(
        client,
        sns_topic_arn="arn:aws:sns:us-east-1:123456789012:credits",
        max_attempts=max_attempts,
        sleep=clock.sleep,
        clock=clock,
    ).send(body)

    expected_calls = min(failures + 1, max_attempts)
    all_failed = failures >= max_attempts

    assert len(client.calls) == expected_calls
    assert result.attempts == expected_calls
    assert result.delivery_failed is all_failed
    assert result.sent is (not all_failed)
    assert (result.failure_reason is not None) is all_failed

    # Every attempt publishes the body verbatim: no footer, no attachment note.
    for name, kwargs in client.calls:
        assert name == "publish"
        assert kwargs["Message"] == body
