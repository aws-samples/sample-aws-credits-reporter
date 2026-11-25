"""Unit tests for Category_Retriever and the CostCategory parser.

Pagination and API-error cases use a botocore ``Stubber`` on a real Cost
Explorer client so request and response shapes are validated against the
service model. Timeout cases use a small fake client, since ``Stubber``
cannot raise ``ReadTimeoutError``. No real AWS account is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError
from botocore.stub import Stubber

from aws_credits_reporter.category_retriever import (
    CALL_TIMEOUT_SECONDS,
    CategoryRetrievalOutcome,
    CategoryRetriever,
    make_cost_explorer_client,
    make_cost_explorer_config,
)
from aws_credits_reporter.models import (
    COST_CATEGORY_FIELDS,
    CostCategory,
    parse_cost_categories,
    parse_cost_category,
)

FIXTURES = Path(__file__).parent / "fixtures"


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

    def list_cost_category_definitions(self, **params):
        self.calls.append(params)
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _read_timeout() -> ReadTimeoutError:
    return ReadTimeoutError(endpoint_url="https://ce.us-east-1.amazonaws.com")


# --------------------------------------------------------------------------
# Client configuration (Req 12.1, 12.5)
# --------------------------------------------------------------------------


def test_client_pinned_to_us_east_1_with_30s_read_timeout(ce_client):
    # Cost Explorer is a global service: botocore reports the partition
    # region ``aws-global`` but resolves the us-east-1 endpoint (Req 12.1).
    assert ce_client.meta.endpoint_url == "https://ce.us-east-1.amazonaws.com"
    assert ce_client.meta.config.read_timeout == CALL_TIMEOUT_SECONDS == 30
    assert ce_client.meta.config.connect_timeout == 30


def test_config_disables_botocore_retries_so_attempt_counts_are_deterministic():
    config = make_cost_explorer_config()
    assert config.retries == {"max_attempts": 1, "mode": "standard"}


# --------------------------------------------------------------------------
# Pagination (Req 12.3)
# --------------------------------------------------------------------------


def test_two_page_pagination_returns_union_and_stops_when_no_token(ce_client):
    page1 = _load("list_cost_category_definitions_page1.json")
    page2 = _load("list_cost_category_definitions_page2.json")
    stubber = Stubber(ce_client)
    stubber.add_response("list_cost_category_definitions", page1, expected_params={})
    stubber.add_response(
        "list_cost_category_definitions",
        page2,
        expected_params={"NextToken": "page-2-token"},
    )
    stubber.activate()

    outcome = CategoryRetriever(ce_client, sleep=lambda _: None).retrieve()

    stubber.assert_no_pending_responses()
    assert outcome.error_message is None
    assert outcome.failed is False
    assert outcome.raw_pages == [page1, page2]
    arns = [c.CostCategoryArn for c in outcome.categories]
    assert arns == [
        ref["CostCategoryArn"] for ref in page1["CostCategoryReferences"]
    ] + [ref["CostCategoryArn"] for ref in page2["CostCategoryReferences"]]


def test_single_page_without_token_makes_exactly_one_call(ce_client):
    page = _load("list_cost_category_definitions.json")
    stubber = Stubber(ce_client)
    stubber.add_response("list_cost_category_definitions", page, expected_params={})
    stubber.activate()

    outcome = CategoryRetriever(ce_client, sleep=lambda _: None).retrieve()

    stubber.assert_no_pending_responses()
    assert outcome.raw_pages == [page]
    assert len(outcome.categories) == 3
    sharing = [c for c in outcome.categories if "billing:creditsharing" in c.SupportedResourceTypes]
    assert [c.Name for c in sharing] == ["CreditSharingGroups"]


def test_raw_pages_exclude_response_metadata():
    page = _load("list_cost_category_definitions.json")
    fake = FakeCeClient([{**page, "ResponseMetadata": {"HTTPStatusCode": 200}}])

    outcome = CategoryRetriever(fake, sleep=lambda _: None).retrieve()

    assert outcome.raw_pages == [page]


# --------------------------------------------------------------------------
# Retry, timeout, and persistent failure (Req 12.5, 12.6)
# --------------------------------------------------------------------------


def test_retryable_client_error_then_success_uses_exponential_backoff(ce_client):
    page = _load("list_cost_category_definitions.json")
    stubber = Stubber(ce_client)
    stubber.add_client_error(
        "list_cost_category_definitions",
        service_error_code="LimitExceededException",
        service_message="slow down",
        http_status_code=429,
    )
    stubber.add_client_error(
        "list_cost_category_definitions",
        service_error_code="InternalServerException",
        service_message="boom",
        http_status_code=500,
    )
    stubber.add_response("list_cost_category_definitions", page, expected_params={})
    stubber.activate()
    sleeps: list[float] = []

    outcome = CategoryRetriever(ce_client, sleep=sleeps.append).retrieve()

    stubber.assert_no_pending_responses()
    assert outcome.error_message is None
    assert len(outcome.categories) == 3
    assert sleeps == [0.5, 1.0]


def test_persistent_client_error_returns_empty_list_plus_error_without_raising(ce_client):
    stubber = Stubber(ce_client)
    for _ in range(3):
        stubber.add_client_error(
            "list_cost_category_definitions",
            service_error_code="AccessDeniedException",
            service_message="not authorized to ce:ListCostCategoryDefinitions",
            http_status_code=403,
        )
    stubber.activate()

    outcome = CategoryRetriever(ce_client, sleep=lambda _: None).retrieve()

    stubber.assert_no_pending_responses()
    assert isinstance(outcome, CategoryRetrievalOutcome)
    assert outcome.categories == []
    assert outcome.raw_pages == []
    assert outcome.failed is True
    assert "after 3 attempts" in outcome.error_message
    assert "AccessDeniedException" in outcome.error_message
    assert "not authorized to ce:ListCostCategoryDefinitions" in outcome.error_message


def test_timeout_counts_as_failed_attempt_then_success():
    page = _load("list_cost_category_definitions.json")
    fake = FakeCeClient([_read_timeout(), page])
    sleeps: list[float] = []

    outcome = CategoryRetriever(fake, sleep=sleeps.append).retrieve()

    assert len(fake.calls) == 2
    assert sleeps == [0.5]
    assert outcome.error_message is None
    assert len(outcome.categories) == 3


def test_three_timeouts_exhaust_attempts_and_record_error():
    fake = FakeCeClient([_read_timeout(), _read_timeout(), _read_timeout(), "never reached"])

    outcome = CategoryRetriever(fake, sleep=lambda _: None).retrieve()

    assert len(fake.calls) == 3
    assert outcome.categories == []
    assert outcome.raw_pages == []
    assert "after 3 attempts" in outcome.error_message
    assert "30s timeout" in outcome.error_message


def test_connect_timeout_also_counts_as_failed_attempt():
    page = _load("list_cost_category_definitions.json")
    fake = FakeCeClient(
        [ConnectTimeoutError(endpoint_url="https://ce.us-east-1.amazonaws.com"), page]
    )

    outcome = CategoryRetriever(fake, sleep=lambda _: None).retrieve()

    assert len(fake.calls) == 2
    assert outcome.error_message is None


def test_failure_on_second_page_yields_zero_definitions_and_error(ce_client):
    """A run must continue with a zero-definition set, not a partial one."""
    page1 = _load("list_cost_category_definitions_page1.json")
    stubber = Stubber(ce_client)
    stubber.add_response("list_cost_category_definitions", page1, expected_params={})
    for _ in range(3):
        stubber.add_client_error(
            "list_cost_category_definitions",
            service_error_code="InternalServerException",
            service_message="boom",
            http_status_code=500,
            expected_params={"NextToken": "page-2-token"},
        )
    stubber.activate()

    outcome = CategoryRetriever(ce_client, sleep=lambda _: None).retrieve()

    stubber.assert_no_pending_responses()
    assert outcome.categories == []
    assert outcome.raw_pages == []
    assert outcome.failed is True


def test_max_retries_is_honoured():
    fake = FakeCeClient([_read_timeout()] * 5)

    outcome = CategoryRetriever(fake, max_retries=5, sleep=lambda _: None).retrieve()

    assert len(fake.calls) == 5
    assert "after 5 attempts" in outcome.error_message


# --------------------------------------------------------------------------
# CostCategory parsing (Req 12.2, 12.4)
# --------------------------------------------------------------------------


def test_parse_full_record_populates_every_field():
    raw = _load("list_cost_category_definitions.json")["CostCategoryReferences"][0]

    cat = parse_cost_category(raw)

    assert cat.CostCategoryArn == raw["CostCategoryArn"]
    assert cat.Name == "CreditSharingGroups"
    assert cat.EffectiveStart == "2024-01-01T00:00:00Z"
    assert cat.NumberOfRules == 3
    assert cat.ProcessingStatus == [{"Component": "COST_EXPLORER", "Status": "APPLIED"}]
    assert cat.Values == ["Platform", "DataScience", "Sandbox"]
    assert cat.DefaultValue == "Unallocated"
    assert cat.SupportedResourceTypes == ["billing:creditsharing"]
    assert cat.missing_fields == []


def test_parse_record_with_omitted_fields_treats_them_as_absent():
    raw = _load("list_cost_category_definitions.json")["CostCategoryReferences"][2]

    cat = parse_cost_category(raw)

    assert cat.Name is None
    assert cat.NumberOfRules is None
    assert cat.DefaultValue is None
    assert cat.ProcessingStatus == []
    assert cat.SupportedResourceTypes == []
    assert cat.Values == ["TeamA"]
    assert cat.missing_fields == [
        "Name",
        "NumberOfRules",
        "ProcessingStatus",
        "DefaultValue",
        "SupportedResourceTypes",
    ]


def test_parse_empty_supported_resource_types_is_present_not_missing():
    raw = _load("list_cost_category_definitions.json")["CostCategoryReferences"][1]

    cat = parse_cost_category(raw)

    assert cat.SupportedResourceTypes == []
    assert "SupportedResourceTypes" not in cat.missing_fields


def test_parse_unparsable_values_are_recorded_as_missing():
    raw = {
        "CostCategoryArn": 123,
        "Name": ["not", "a", "string"],
        "NumberOfRules": True,
        "ProcessingStatus": "APPLIED",
        "Values": [1, 2],
        "SupportedResourceTypes": "billing:creditsharing",
    }

    cat = parse_cost_category(raw)

    assert cat == CostCategory(missing_fields=list(COST_CATEGORY_FIELDS))


def test_parse_non_dict_records_all_fields_missing_and_never_raises():
    for raw in (None, "text", 7, ["list"]):
        assert parse_cost_category(raw).missing_fields == list(COST_CATEGORY_FIELDS)


def test_parse_cost_categories_flattens_pages_and_skips_bad_shapes():
    page1 = _load("list_cost_category_definitions_page1.json")
    page2 = _load("list_cost_category_definitions_page2.json")

    cats = parse_cost_categories([page1, "junk", {"CostCategoryReferences": "no"}, page2])

    assert [c.CostCategoryArn for c in cats] == [
        page1["CostCategoryReferences"][0]["CostCategoryArn"],
        page1["CostCategoryReferences"][1]["CostCategoryArn"],
        page2["CostCategoryReferences"][0]["CostCategoryArn"],
    ]
    assert parse_cost_categories([]) == []


# --------------------------------------------------------------------------
# Category_Retriever -> Snapshot handoff (Req 13.7)
#
# The unmodified raw_pages a successful retrieval produces are exactly what a
# Phase 5 run stores in Snapshot.raw_cost_categories, so they must survive the
# snapshot serialization round trip byte-for-byte.
# --------------------------------------------------------------------------


def test_retrieved_raw_pages_round_trip_through_snapshot_serialization():
    page1 = _load("list_cost_category_definitions_page1.json")
    page2 = _load("list_cost_category_definitions_page2.json")
    fake = FakeCeClient(
        [
            {**page1, "ResponseMetadata": {"HTTPStatusCode": 200}},
            {**page2, "ResponseMetadata": {"HTTPStatusCode": 200}},
        ]
    )

    outcome = CategoryRetriever(fake, sleep=lambda _: None).retrieve()

    # Deferred import keeps the retriever tests independent of the store module.
    from datetime import datetime, timezone

    from aws_credits_reporter.models import Snapshot
    from aws_credits_reporter.snapshot_store import SnapshotStore

    assert outcome.raw_pages == [page1, page2]

    snapshot = Snapshot(
        key="",
        taken_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        raw_cost_categories=outcome.raw_pages,
    )
    restored = SnapshotStore.deserialize(
        "snapshots/x.json", SnapshotStore.serialize(snapshot)
    )

    assert restored.raw_cost_categories == [page1, page2]
