"""Credit_Retriever: retrieve all organization credits from AWS Billing.

Calls the Billing ``GetCredits`` API in ``us-east-1`` with the payer-account
flag enabled, follows pagination tokens while present, retries retryable
errors with exponential backoff, and surfaces authorization failures with the
name of the missing permission.

Service model notes (botocore 1.43.x, verified against the AWS API reference):

- ``GetCredits`` input members: ``accountId`` (required), ``startDate``
  (required, timestamp), ``endDate`` (timestamp), ``payerAccountFlag`` (bool).
- ``GetCredits`` output members: ``credits`` only. The current model exposes no
  pagination token. The loop below is written generically so that, should the
  service add ``nextToken`` (or a stub returns one), every page is followed;
  with the current model it performs exactly one call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from .aws_calls import call_with_backoff, client_error_details, is_authorization_error
from .models import Credit, parse_credits

BILLING_REGION = "us-east-1"
DEFAULT_START_DATE = date(2020, 1, 1)
REQUIRED_PERMISSION = "billing:GetCredits"

# Exact member names from the ``billing`` service model.
PARAM_ACCOUNT_ID = "accountId"
PARAM_START_DATE = "startDate"
PARAM_PAYER_FLAG = "payerAccountFlag"
PARAM_NEXT_TOKEN = "nextToken"  # input token name (not in the current model)
RESPONSE_NEXT_TOKEN = "nextToken"  # output token name (not in the current model)

RETRYABLE_ERROR_CODES = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "InternalServerException",
        "ServiceUnavailable",
        "InternalFailure",
    }
)
RETRYABLE_BOTOCORE_ERRORS = (EndpointConnectionError, ConnectionClosedError, ReadTimeoutError)


class RetrievalError(Exception):
    """Fatal retrieval failure carrying the API error message."""


class AuthorizationError(RetrievalError):
    """The caller lacks the permission required to call ``GetCredits``."""


@dataclass
class RetrievalOutcome:
    """Result of one successful ``retrieve()`` call.

    ``raw_pages`` holds the unmodified response dicts (minus
    ``ResponseMetadata``) in the order received, for the snapshot.
    ``credits`` is the parsed, flattened list.
    """

    raw_pages: list[dict] = field(default_factory=list)
    credits: list[Credit] = field(default_factory=list)


def make_billing_client(session: boto3.Session | None = None):
    """Return a Billing client pinned to ``us-east-1``."""
    if session is None:
        return boto3.client("billing", region_name=BILLING_REGION)
    return session.client("billing", region_name=BILLING_REGION)


def resolve_account_id(session: boto3.Session | None = None) -> str:
    """Return the caller's account id via STS (``GetCredits`` requires it)."""
    sts = (session or boto3).client("sts", region_name=BILLING_REGION)
    return sts.get_caller_identity()["Account"]


def is_retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, RETRYABLE_BOTOCORE_ERRORS):
        return True
    if not isinstance(exc, ClientError):
        return False
    code, status, _ = client_error_details(exc)
    if code in RETRYABLE_ERROR_CODES:
        return True
    return isinstance(status, int) and 500 <= status <= 599


class CreditRetriever:
    """Retrieve all credits for the payer account, paginated, with retry."""

    def __init__(
        self,
        billing_client,
        max_retries: int = 5,
        sleep: Callable[[float], Any] = time.sleep,
        account_id: str | None = None,
        start_date: date = DEFAULT_START_DATE,
    ):
        self._client = billing_client
        self._max_retries = max(1, int(max_retries))
        self._sleep = sleep
        self._account_id = account_id
        self._start_date = start_date

    def _base_params(self) -> dict:
        params: dict[str, Any] = {
            PARAM_PAYER_FLAG: True,
            PARAM_START_DATE: self._start_date.isoformat(),
        }
        if self._account_id:
            params[PARAM_ACCOUNT_ID] = self._account_id
        return params

    def _call_with_retry(self, params: dict) -> dict:
        """Call ``get_credits`` once per attempt, up to ``max_retries`` total."""
        try:
            return call_with_backoff(
                lambda: self._client.get_credits(**params),
                attempts=self._max_retries,
                is_retryable=is_retryable_error,
                sleep=self._sleep,
            )
        except ClientError as exc:
            code, _, message = client_error_details(exc)
            if is_authorization_error(exc):
                raise AuthorizationError(
                    f"Authorization failure calling GetCredits: missing permission "
                    f"{REQUIRED_PERMISSION}. API message: {message}"
                ) from exc
            if not is_retryable_error(exc):
                raise RetrievalError(f"GetCredits failed ({code}): {message}") from exc
            raise RetrievalError(
                f"GetCredits failed after {self._max_retries} attempts ({code}): {message}"
            ) from exc
        except RETRYABLE_BOTOCORE_ERRORS as exc:
            raise RetrievalError(
                f"GetCredits failed after {self._max_retries} attempts {exc}"
            ) from exc
        except BotoCoreError as exc:
            raise RetrievalError(f"GetCredits failed: {exc}") from exc

    def retrieve(self) -> RetrievalOutcome:
        """Retrieve every page of credits and parse them."""
        raw_pages: list[dict] = []
        params = self._base_params()
        while True:
            response = self._call_with_retry(params)
            page = {k: v for k, v in response.items() if k != "ResponseMetadata"}
            raw_pages.append(page)
            token = page.get(RESPONSE_NEXT_TOKEN)
            if not token:
                break
            params = {**self._base_params(), PARAM_NEXT_TOKEN: token}
        return RetrievalOutcome(raw_pages=raw_pages, credits=parse_credits(raw_pages))
