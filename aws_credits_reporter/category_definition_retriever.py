"""Category_Definition_Retriever: describe one cost category definition.

Calls the Cost Explorer ``DescribeCostCategoryDefinition`` API in
``us-east-1`` for a single Bound_Cost_Category ARN, abandons any call that
exceeds the 30 second read timeout and counts it as a failed attempt, and
retries up to 3 attempts with exponential backoff. Mirrors the resilience
pattern of ``Category_Retriever``: persistent failure is *not* fatal. On
persistent failure or an authorization error (``AccessDeniedException``) the
error is recorded on ``messages`` and ``None`` is returned, so the run
continues and the member accounts for that ARN are reported unavailable
.

Each distinct ARN is described at most once per run: results (including a
failed ``None``) are memoized so a repeated ARN never calls the API again.

Service model notes (botocore 1.43.x):

- ``DescribeCostCategoryDefinition`` input members: ``CostCategoryArn`` and
  ``EffectiveOn``.
- Output member: ``CostCategory``, carrying ``Name``, ``Rules``,
  ``ProcessingStatus`` (a list of ``{"Component", "Status"}`` dicts), etc.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
)

from .aws_calls import call_with_backoff
from .category_retriever import DEFAULT_MAX_ATTEMPTS, describe_error
from .models import CostCategoryDefinition

# Exact member names from the ``ce`` service model.
PARAM_COST_CATEGORY_ARN = "CostCategoryArn"
RESPONSE_DEFINITION = "CostCategory"

# A category whose every processing component reports this status is fully
# applied; any other status (e.g. ``PROCESSING``) is accepted as valid but
# leaves ``processing_complete`` False.
PROCESSING_STATUS_COMPLETE = "APPLIED"


def _processing_complete(processing_status: Any) -> bool:
    """True only when every processing component reports ``APPLIED``.

    ``ProcessingStatus`` is a list of ``{"Component", "Status"}`` dicts. An
    empty or unexpected shape is treated as not-yet-complete rather than
    raising; the definition's rules are still read regardless.
    """
    if not isinstance(processing_status, list) or not processing_status:
        return False
    return all(
        isinstance(item, dict) and item.get("Status") == PROCESSING_STATUS_COMPLETE
        for item in processing_status
    )


class CategoryDefinitionRetriever:
    """Describe cost category definitions by ARN, with retry and memoization.

    Accepts an injected Cost Explorer client so tests can pass stubs or fakes;
    use ``make_cost_explorer_client`` to build the real one with the 30 second
    timeout configuration. Each distinct ARN is described at most once per
    run; recorded error messages accumulate on ``messages``.
    """

    def __init__(
        self,
        ce_client,
        max_retries: int = DEFAULT_MAX_ATTEMPTS,
        sleep: Callable[[float], Any] = time.sleep,
    ):
        self._client = ce_client
        # ``max_retries`` is the total number of attempts per call, matching
        # the Category_Retriever convention.
        self._max_attempts = max(1, int(max_retries))
        self._sleep = sleep
        # Memoize per ARN so each distinct ARN is described at most once per
        # run. A failed describe caches ``None``.
        self._cache: dict[str, CostCategoryDefinition | None] = {}
        self.messages: list[str] = []

    def _call_with_retry(self, arn: str) -> dict:
        """Call ``describe_cost_category_definition`` up to ``max_attempts`` times.

        Every error from the API, including a timed-out call, counts as one
        failed attempt. Raises the last error once all
        attempts are exhausted; ``describe()`` converts that into a recorded
        message rather than propagating it.
        """
        return call_with_backoff(
            lambda: self._client.describe_cost_category_definition(
                **{PARAM_COST_CATEGORY_ARN: arn}
            ),
            attempts=self._max_attempts,
            is_retryable=lambda exc: isinstance(exc, (ClientError, BotoCoreError)),
            sleep=self._sleep,
        )

    def describe(self, cost_category_arn: str) -> CostCategoryDefinition | None:
        """Describe one cost category ARN, returning its full definition.

        Never raises for API failures. On persistent failure or an
        authorization error the error is recorded on ``messages`` and ``None``
        is returned so the caller reports that ARN's member accounts as
        unavailable while the run continues. The result is
        memoized per ARN, so a repeated ARN returns the cached value without a
        second API call.
        """
        if cost_category_arn in self._cache:
            return self._cache[cost_category_arn]

        try:
            response = self._call_with_retry(cost_category_arn)
        except (ClientError, BotoCoreError) as exc:
            message = (
                f"DescribeCostCategoryDefinition for {cost_category_arn} failed "
                f"after {self._max_attempts} attempts {describe_error(exc)}; "
                f"member accounts for this ARN are unavailable"
            )
            self.messages.append(message)
            self._cache[cost_category_arn] = None
            return None

        definition = self._parse(cost_category_arn, response)
        self._cache[cost_category_arn] = definition
        return definition

    @staticmethod
    def _parse(arn: str, response: dict) -> CostCategoryDefinition:
        """Parse a ``DescribeCostCategoryDefinition`` response into a definition.

        A non-complete ``ProcessingStatus`` is accepted: rules are read
        regardless and ``processing_complete`` reflects whether every
        component is applied.
        """
        payload = response.get(RESPONSE_DEFINITION)
        if not isinstance(payload, dict):
            payload = {}
        name = payload.get("Name")
        name = name if isinstance(name, str) else None
        rules = payload.get("Rules")
        rules = [dict(r) for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []
        return CostCategoryDefinition(
            arn=arn,
            name=name,
            rules=rules,
            processing_complete=_processing_complete(payload.get("ProcessingStatus")),
        )
