"""Unit tests for Credit_Retriever with mocked boto3 responses.

Retry, non-retryable-error, and authorization cases use a botocore ``Stubber``
on a real Billing client so request and response shapes are validated against
the service model. Pagination uses a small fake client, because the current
``GetCredits`` service model exposes no ``nextToken`` output member and
``Stubber`` would reject a response carrying one. No real AWS account is
touched (Requirements 1.1, 1.3, 1.4, 1.5, 1.7).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber

from aws_credits_reporter.credit_retriever import (
    REQUIRED_PERMISSION,
    AuthorizationError,
    CreditRetriever,
    RetrievalError,
    RetrievalOutcome,
    make_billing_client,
)

FIXTURES = Path(__file__).parent / "fixtures"
ACCOUNT_ID = "111111111111"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def billing_client(aws_env):
    return make_billing_client()


class FakeBillingClient:
    """Minimal fake that returns/raises a scripted sequence of steps.

    A ``dict`` step is returned as a ``get_credits`` response; a
    ``BaseException`` step is raised. Every call's params are recorded so
    pagination tokens can be asserted.
    """

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

    def get_credits(self, **params):
        self.calls.append(params)
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


# --------------------------------------------------------------------------
# Pagination (Req 1.1, 1.3)
# --------------------------------------------------------------------------


def test_two_page_pagination_returns_union_and_stops_when_no_token():
    page1 = _load("get_credits_page1.json")
    page2 = _load("get_credits_page2.json")
    # page1 carries "nextToken": "page2-token"; page2 carries none.
    fake = FakeBillingClient([page1, page2])

    outcome = CreditRetriever(fake, account_id=ACCOUNT_ID, sleep=lambda _: None).retrieve()

    # Exactly one call per page, stopping when the second page returns no token.
    assert len(fake.calls) == 2
    # First call carries the base params (payer flag, start date, account).
    assert fake.calls[0] == {
        "payerAccountFlag": True,
        "startDate": "2020-01-01",
        "accountId": ACCOUNT_ID,
    }
    # Second call forwards the token from page1.
    assert fake.calls[1]["nextToken"] == "page2-token"

    # Raw pages are the unmodified fixture pages in order (for the snapshot).
    assert outcome.raw_pages == [page1, page2]

    # Credits are the concatenation of both pages in order (Req 1.3).
    expected_ids = [c["creditId"] for c in page1["credits"]] + [
        c["creditId"] for c in page2["credits"]
    ]
    assert [c.creditId for c in outcome.credits] == expected_ids
    assert len(outcome.credits) == 3


def test_single_page_without_token_makes_exactly_one_call():
    page = _load("get_credits_page1.json")
    page.pop("nextToken", None)
    fake = FakeBillingClient([page])

    outcome = CreditRetriever(fake, account_id=ACCOUNT_ID, sleep=lambda _: None).retrieve()

    assert len(fake.calls) == 1
    assert outcome.raw_pages == [page]
    assert [c.creditId for c in outcome.credits] == [c["creditId"] for c in page["credits"]]


def test_zero_credits_returns_empty_outcome():
    fake = FakeBillingClient([{"credits": []}])

    outcome = CreditRetriever(fake, account_id=ACCOUNT_ID, sleep=lambda _: None).retrieve()

    assert isinstance(outcome, RetrievalOutcome)
    assert outcome.credits == []
    assert outcome.raw_pages == [{"credits": []}]


def test_raw_pages_exclude_response_metadata():
    page = _load("get_credits_page1.json")
    page.pop("nextToken", None)
    fake = FakeBillingClient([{**page, "ResponseMetadata": {"HTTPStatusCode": 200}}])

    outcome = CreditRetriever(fake, account_id=ACCOUNT_ID, sleep=lambda _: None).retrieve()

    assert outcome.raw_pages == [page]


# --------------------------------------------------------------------------
# Retry on retryable errors (Req 1.4)
# --------------------------------------------------------------------------


def test_retryable_error_succeeds_on_attempt_before_fifth():
    """A retryable error on attempts 1..4 is retried; attempt 5 succeeds."""
    page = _load("get_credits_page1.json")
    page.pop("nextToken", None)
    fake = FakeBillingClient(
        [EndpointConnectionError(endpoint_url="https://billing.us-east-1.amazonaws.com")] * 4
        + [page]
    )
    sleeps: list[float] = []

    outcome = CreditRetriever(
        fake, account_id=ACCOUNT_ID, max_retries=5, sleep=sleeps.append
    ).retrieve()

    assert len(fake.calls) == 5
    # Exponential backoff between the four failed attempts (Req 1.4).
    assert sleeps == [0.5, 1.0, 2.0, 4.0]
    assert [c.creditId for c in outcome.credits] == [c["creditId"] for c in page["credits"]]


def test_retryable_client_error_5xx_then_success_uses_exponential_backoff(billing_client):
    page = _load("get_credits_page1.json")
    page.pop("nextToken", None)
    stubber = Stubber(billing_client)
    stubber.add_client_error(
        "get_credits",
        service_error_code="ThrottlingException",
        service_message="slow down",
        http_status_code=429,
    )
    stubber.add_client_error(
        "get_credits",
        service_error_code="InternalServerException",
        service_message="boom",
        http_status_code=500,
    )
    stubber.add_response("get_credits", page)
    stubber.activate()
    sleeps: list[float] = []

    outcome = CreditRetriever(
        billing_client, account_id=ACCOUNT_ID, sleep=sleeps.append
    ).retrieve()

    stubber.assert_no_pending_responses()
    assert sleeps == [0.5, 1.0]
    assert [c.creditId for c in outcome.credits] == [c["creditId"] for c in page["credits"]]


def test_retryable_error_on_all_five_attempts_raises_retrieval_error():
    fake = FakeBillingClient(
        [EndpointConnectionError(endpoint_url="https://billing.us-east-1.amazonaws.com")] * 5
    )

    retriever = CreditRetriever(fake, account_id=ACCOUNT_ID, max_retries=5, sleep=lambda _: None)
    with pytest.raises(RetrievalError) as exc_info:
        retriever.retrieve()

    assert len(fake.calls) == 5
    assert "after 5 attempts" in str(exc_info.value)
    # A persistent authorization failure is fatal but must not be one:
    assert not isinstance(exc_info.value, AuthorizationError)


def test_persistent_retryable_client_error_raises_after_five_attempts(billing_client):
    stubber = Stubber(billing_client)
    for _ in range(5):
        stubber.add_client_error(
            "get_credits",
            service_error_code="InternalServerException",
            service_message="boom",
            http_status_code=500,
        )
    stubber.activate()

    retriever = CreditRetriever(billing_client, account_id=ACCOUNT_ID, sleep=lambda _: None)
    with pytest.raises(RetrievalError) as exc_info:
        retriever.retrieve()

    stubber.assert_no_pending_responses()
    message = str(exc_info.value)
    assert "after 5 attempts" in message
    assert "InternalServerException" in message
    assert "boom" in message


# --------------------------------------------------------------------------
# Non-retryable errors raise immediately (Req 1.5)
# --------------------------------------------------------------------------


def test_non_retryable_error_raises_immediately_without_retry(billing_client):
    stubber = Stubber(billing_client)
    stubber.add_client_error(
        "get_credits",
        service_error_code="ValidationException",
        service_message="bad startDate",
        http_status_code=400,
    )
    stubber.activate()

    retriever = CreditRetriever(billing_client, account_id=ACCOUNT_ID, sleep=lambda _: None)
    with pytest.raises(RetrievalError) as exc_info:
        retriever.retrieve()

    # No pending responses => exactly one call was made, no retry.
    stubber.assert_no_pending_responses()
    message = str(exc_info.value)
    assert "ValidationException" in message
    assert "bad startDate" in message
    assert not isinstance(exc_info.value, AuthorizationError)


def test_non_retryable_error_makes_exactly_one_call():
    from botocore.exceptions import ClientError

    error = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad request"}},
        "GetCredits",
    )
    fake = FakeBillingClient([error])

    retriever = CreditRetriever(fake, account_id=ACCOUNT_ID, sleep=lambda _: None)
    with pytest.raises(RetrievalError):
        retriever.retrieve()

    assert len(fake.calls) == 1


# --------------------------------------------------------------------------
# Authorization failures (Req 1.7)
# --------------------------------------------------------------------------


def test_access_denied_raises_authorization_error_naming_permission(billing_client):
    stubber = Stubber(billing_client)
    stubber.add_client_error(
        "get_credits",
        service_error_code="AccessDeniedException",
        service_message="User is not authorized to perform billing:GetCredits",
        http_status_code=403,
    )
    stubber.activate()

    retriever = CreditRetriever(billing_client, account_id=ACCOUNT_ID, sleep=lambda _: None)
    with pytest.raises(AuthorizationError) as exc_info:
        retriever.retrieve()

    stubber.assert_no_pending_responses()
    message = str(exc_info.value)
    assert REQUIRED_PERMISSION == "billing:GetCredits"
    assert REQUIRED_PERMISSION in message


def test_authorization_error_is_not_retried():
    from botocore.exceptions import ClientError

    error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "GetCredits",
    )
    fake = FakeBillingClient([error, error])

    retriever = CreditRetriever(fake, account_id=ACCOUNT_ID, max_retries=5, sleep=lambda _: None)
    with pytest.raises(AuthorizationError):
        retriever.retrieve()

    # Authorization failures are fatal on the first attempt (Req 1.7).
    assert len(fake.calls) == 1


def test_unauthorized_operation_code_also_maps_to_authorization_error():
    from botocore.exceptions import ClientError

    error = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "no access"}},
        "GetCredits",
    )
    fake = FakeBillingClient([error])

    retriever = CreditRetriever(fake, account_id=ACCOUNT_ID, sleep=lambda _: None)
    with pytest.raises(AuthorizationError) as exc_info:
        retriever.retrieve()

    assert len(fake.calls) == 1
    assert REQUIRED_PERMISSION in str(exc_info.value)
