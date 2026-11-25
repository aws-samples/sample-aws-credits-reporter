"""Unit tests for Category_Definition_Retriever.

Describe-call success and API-error cases use a botocore ``Stubber`` on a real
Cost Explorer client so request and response shapes are validated against the
service model. Timeout cases use a small fake client, since ``Stubber`` cannot
raise ``ReadTimeoutError``. No real AWS account is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from botocore.exceptions import ReadTimeoutError
from botocore.stub import Stubber

from aws_credits_reporter.category_definition_retriever import CategoryDefinitionRetriever
from aws_credits_reporter.category_retriever import CALL_TIMEOUT_SECONDS, make_cost_explorer_client
from aws_credits_reporter.models import CostCategoryDefinition

FIXTURES = Path(__file__).parent / "fixtures"

ARN = "arn:aws:ce::111111111111:costcategory/11111111-aaaa-bbbb-cccc-000000000010"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def ce_client(aws_env):
    return make_cost_explorer_client()


class FakeCeClient:
    """Minimal fake that raises a scripted sequence of exceptions, then returns."""

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

    def describe_cost_category_definition(self, **params):
        self.calls.append(params)
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _read_timeout() -> ReadTimeoutError:
    return ReadTimeoutError(endpoint_url="https://ce.us-east-1.amazonaws.com")


# --------------------------------------------------------------------------
# Client configuration
# --------------------------------------------------------------------------


def test_client_pinned_to_us_east_1_with_30s_read_timeout(ce_client):
    assert ce_client.meta.endpoint_url == "https://ce.us-east-1.amazonaws.com"
    assert ce_client.meta.config.read_timeout == CALL_TIMEOUT_SECONDS == 30
    assert ce_client.meta.config.connect_timeout == 30


# --------------------------------------------------------------------------
# Success: parse rules and dimension values (Req 19.1)
# --------------------------------------------------------------------------


def test_success_parses_rules_and_dimension_values(ce_client):
    response = _load("describe_cost_category_definition.json")
    stubber = Stubber(ce_client)
    stubber.add_response(
        "describe_cost_category_definition",
        response,
        expected_params={"CostCategoryArn": ARN},
    )
    stubber.activate()

    definition = CategoryDefinitionRetriever(ce_client, sleep=lambda _: None).describe(ARN)

    stubber.assert_no_pending_responses()
    assert isinstance(definition, CostCategoryDefinition)
    assert definition.arn == ARN
    assert definition.name == "CreditGroup"
    assert len(definition.rules) == 1
    rule = definition.rules[0]
    assert rule["Value"] == "Credit_Rule"
    dimensions = rule["Rule"]["Dimensions"]
    assert dimensions["Key"] == "LINKED_ACCOUNT"
    assert dimensions["Values"] == ["444444444444", "555555555555"]


# --------------------------------------------------------------------------
# PROCESSING status still yields rules (Req 19.6)
# --------------------------------------------------------------------------


def test_processing_status_still_yields_rules_with_processing_complete_false(ce_client):
    # The fixture's ProcessingStatus component is PROCESSING, not APPLIED.
    response = _load("describe_cost_category_definition.json")
    stubber = Stubber(ce_client)
    stubber.add_response(
        "describe_cost_category_definition",
        response,
        expected_params={"CostCategoryArn": ARN},
    )
    stubber.activate()

    definition = CategoryDefinitionRetriever(ce_client, sleep=lambda _: None).describe(ARN)

    stubber.assert_no_pending_responses()
    assert definition is not None
    assert definition.processing_complete is False
    assert len(definition.rules) == 1


def test_fully_applied_processing_status_sets_processing_complete_true():
    response = _load("describe_cost_category_definition.json")
    applied = {
        **response,
        "CostCategory": {
            **response["CostCategory"],
            "ProcessingStatus": [{"Component": "COST_EXPLORER", "Status": "APPLIED"}],
        },
    }
    fake = FakeCeClient([applied])

    definition = CategoryDefinitionRetriever(fake, sleep=lambda _: None).describe(ARN)

    assert definition is not None
    assert definition.processing_complete is True


# --------------------------------------------------------------------------
# Timeout counts as a failed attempt, then retried (Req 19.4)
# --------------------------------------------------------------------------


def test_timeout_counts_as_failed_attempt_then_success():
    response = _load("describe_cost_category_definition.json")
    fake = FakeCeClient([_read_timeout(), response])
    sleeps: list[float] = []

    definition = CategoryDefinitionRetriever(fake, sleep=sleeps.append).describe(ARN)

    assert len(fake.calls) == 2
    assert sleeps == [0.5]
    assert definition is not None
    assert definition.name == "CreditGroup"


# --------------------------------------------------------------------------
# 3-attempt exhaustion returns None with a recorded error (Req 19.4, 19.5)
# --------------------------------------------------------------------------


def test_three_timeouts_exhaust_attempts_return_none_with_recorded_error():
    fake = FakeCeClient([_read_timeout(), _read_timeout(), _read_timeout(), "never reached"])

    retriever = CategoryDefinitionRetriever(fake, sleep=lambda _: None)
    definition = retriever.describe(ARN)

    assert len(fake.calls) == 3
    assert definition is None
    assert len(retriever.messages) == 1
    message = retriever.messages[0]
    assert "after 3 attempts" in message
    assert "30s timeout" in message
    assert ARN in message


# --------------------------------------------------------------------------
# AccessDenied returns None (Req 19.5)
# --------------------------------------------------------------------------


def test_access_denied_returns_none_with_recorded_error(ce_client):
    stubber = Stubber(ce_client)
    for _ in range(3):
        stubber.add_client_error(
            "describe_cost_category_definition",
            service_error_code="AccessDeniedException",
            service_message="not authorized to ce:DescribeCostCategoryDefinition",
            http_status_code=403,
            expected_params={"CostCategoryArn": ARN},
        )
    stubber.activate()

    retriever = CategoryDefinitionRetriever(ce_client, sleep=lambda _: None)
    definition = retriever.describe(ARN)

    stubber.assert_no_pending_responses()
    assert definition is None
    assert len(retriever.messages) == 1
    assert "AccessDeniedException" in retriever.messages[0]
    assert ARN in retriever.messages[0]


# --------------------------------------------------------------------------
# Distinct ARNs described once each; repeated ARN memoized (Req 19.1, 19.2)
# --------------------------------------------------------------------------


def test_distinct_arns_described_once_each_and_repeat_is_memoized():
    response = _load("describe_cost_category_definition.json")
    arn_a = ARN
    arn_b = ARN.replace("000000000010", "000000000020")
    response_b = {
        **response,
        "CostCategory": {**response["CostCategory"], "CostCategoryArn": arn_b},
    }
    fake = FakeCeClient([response, response_b])

    retriever = CategoryDefinitionRetriever(fake, sleep=lambda _: None)

    first_a = retriever.describe(arn_a)
    first_b = retriever.describe(arn_b)
    # Repeated ARNs must not trigger a second API call.
    second_a = retriever.describe(arn_a)
    second_b = retriever.describe(arn_b)

    assert [c["CostCategoryArn"] for c in fake.calls] == [arn_a, arn_b]
    assert first_a is second_a
    assert first_b is second_b
    assert first_a.arn == arn_a
    assert first_b.arn == arn_b


def test_failed_describe_is_memoized_as_none_without_a_second_call():
    fake = FakeCeClient([_read_timeout(), _read_timeout(), _read_timeout()])

    retriever = CategoryDefinitionRetriever(fake, sleep=lambda _: None)

    assert retriever.describe(ARN) is None
    assert retriever.describe(ARN) is None
    # Only the first describe consumed attempts; the cached None short-circuits.
    assert len(fake.calls) == 3
    assert len(retriever.messages) == 1
