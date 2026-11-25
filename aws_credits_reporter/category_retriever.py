"""Category_Retriever: retrieve cost category definitions from Cost Explorer.

Calls the Cost Explorer ``ListCostCategoryDefinitions`` API in ``us-east-1``,
follows ``NextToken`` while present, abandons any single call that exceeds the
30 second read timeout and counts it as a failed attempt, and retries up to 3
attempts with exponential backoff. Unlike ``Credit_Retriever``, persistent
failure is *not* fatal: the error message is recorded on the outcome and an
empty category set is returned so the run can continue
.

Service model notes (botocore 1.43.x):

- ``ListCostCategoryDefinitions`` input members: ``EffectiveOn``,
  ``NextToken``, ``MaxResults``, ``SupportedResourceTypes``.
- Output members: ``CostCategoryReferences`` (list) and ``NextToken``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ReadTimeoutError,
)

from .aws_calls import call_with_backoff, client_error_details
from .models import CostCategory, parse_cost_categories

COST_EXPLORER_REGION = "us-east-1"
CALL_TIMEOUT_SECONDS = 30
DEFAULT_MAX_ATTEMPTS = 3

# Exact member names from the ``ce`` service model.
PARAM_NEXT_TOKEN = "NextToken"
RESPONSE_NEXT_TOKEN = "NextToken"

TIMEOUT_ERRORS = (ReadTimeoutError, ConnectTimeoutError)


@dataclass
class CategoryRetrievalOutcome:
    """Result of one ``retrieve()`` call.

    ``raw_pages`` holds the unmodified response dicts (minus
    ``ResponseMetadata``) in the order received, for the snapshot.
    ``categories`` is the parsed, flattened list. ``error_message`` is set
    when retrieval failed persistently; in that case both lists are empty and
    the caller is expected to continue the run with zero definitions
.
    """

    raw_pages: list[dict] = field(default_factory=list)
    categories: list[CostCategory] = field(default_factory=list)
    error_message: str | None = None

    @property
    def failed(self) -> bool:
        return self.error_message is not None


def make_cost_explorer_config(call_timeout_s: int = CALL_TIMEOUT_SECONDS) -> Config:
    """Return a botocore ``Config`` enforcing the per-call timeout.

    Both the read and connect timeouts are bounded so a hung call is
    abandoned within ``call_timeout_s``. botocore's own
    retry layer is disabled so that the retriever's 3-attempt policy is the
    only one in effect and attempt counts stay deterministic.
    """
    return Config(
        region_name=COST_EXPLORER_REGION,
        read_timeout=call_timeout_s,
        connect_timeout=call_timeout_s,
        retries={"max_attempts": 1, "mode": "standard"},
    )


def make_cost_explorer_client(
    session: boto3.Session | None = None,
    call_timeout_s: int = CALL_TIMEOUT_SECONDS,
):
    """Return a Cost Explorer client pinned to ``us-east-1`` with the timeout config."""
    config = make_cost_explorer_config(call_timeout_s)
    if session is None:
        return boto3.client("ce", region_name=COST_EXPLORER_REGION, config=config)
    return session.client("ce", region_name=COST_EXPLORER_REGION, config=config)


def is_timeout_error(exc: BaseException) -> bool:
    """True when the call was abandoned because it exceeded the timeout."""
    return isinstance(exc, TIMEOUT_ERRORS)


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        code, _, message = client_error_details(exc)
        return f"({code}): {message}"
    if is_timeout_error(exc):
        return f"call exceeded {CALL_TIMEOUT_SECONDS}s timeout: {exc}"
    return str(exc)


class CategoryRetriever:
    """Retrieve all cost category definitions, paginated, with retry.

    Accepts an injected Cost Explorer client so tests can pass stubs or fakes;
    use ``make_cost_explorer_client`` to build the real one with the 30 second
    timeout configuration.
    """

    def __init__(
        self,
        ce_client,
        max_retries: int = DEFAULT_MAX_ATTEMPTS,
        sleep: Callable[[float], Any] = time.sleep,
    ):
        self._client = ce_client
        # ``max_retries`` is the total number of attempts per call, matching
        # the Credit_Retriever convention.
        self._max_attempts = max(1, int(max_retries))
        self._sleep = sleep

    def _call_with_retry(self, params: dict) -> dict:
        """Call ``list_cost_category_definitions`` up to ``max_attempts`` times.

        Every error from the API, including a timed-out call, counts as one
        failed attempt. Raises the last error once
        all attempts are exhausted; ``retrieve()`` converts that into a
        recorded message rather than propagating it.
        """
        return call_with_backoff(
            lambda: self._client.list_cost_category_definitions(**params),
            attempts=self._max_attempts,
            is_retryable=lambda exc: isinstance(exc, (ClientError, BotoCoreError)),
            sleep=self._sleep,
        )

    def retrieve(self) -> CategoryRetrievalOutcome:
        """Retrieve every page of cost category definitions and parse them.

        Never raises for API failures. On persistent failure the outcome
        carries ``error_message`` and empty ``raw_pages`` / ``categories`` so
        the snapshot and the classifier both see a zero-definition set.
        """
        raw_pages: list[dict] = []
        params: dict[str, Any] = {}
        while True:
            try:
                response = self._call_with_retry(params)
            except (ClientError, BotoCoreError) as exc:
                message = (
                    f"ListCostCategoryDefinitions failed after {self._max_attempts} "
                    f"attempts {describe_error(exc)}; continuing with zero cost "
                    f"category definitions"
                )
                return CategoryRetrievalOutcome(error_message=message)

            page = {k: v for k, v in response.items() if k != "ResponseMetadata"}
            raw_pages.append(page)
            token = page.get(RESPONSE_NEXT_TOKEN)
            if not token:
                break
            params = {PARAM_NEXT_TOKEN: token}

        return CategoryRetrievalOutcome(
            raw_pages=raw_pages, categories=parse_cost_categories(raw_pages)
        )
