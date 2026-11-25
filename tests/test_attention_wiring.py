"""Wiring tests for the "NEEDS ATTENTION" block (Requirement 26).

Drives the real ``Orchestrator`` and the real ``Notifier`` over a moto SNS
topic, with in-memory fakes for credits and the snapshot store, so the body
asserted on is the message SNS actually received.

* A credit with a partial binding appears in the published body and on
  ``RunResult.attention``; the run still succeeds (Req 26.1a, 26.6).
* The captured five-credit live scenario, with every name resolved, yields no
  attention items and a body identical to the one built without the
  attention parameter, so today's email is unchanged (Req 26.4, 26.7).

No real AWS account is touched.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

from aws_credits_reporter.category_retriever import CategoryRetrievalOutcome
from aws_credits_reporter.credit_retriever import RetrievalOutcome
from aws_credits_reporter.models import (
    CostCategoryDefinition,
    Snapshot,
    parse_cost_categories,
    parse_credits,
)
from aws_credits_reporter.notifier import Notifier
from aws_credits_reporter.report_body import NEEDS_ATTENTION_HEADING, SHARING_SUMMARY_HEADING, build_dashboard_body
from aws_credits_reporter.attention_builder import REASON_PARTIAL_BINDING
from aws_credits_reporter.orchestrator import Orchestrator, RunResult

FIXTURES = Path(__file__).parent / "fixtures"
REGION = "us-east-1"
NOW = datetime(2026, 5, 15, 8, 0, tzinfo=timezone.utc)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


class FakeRetriever:
    def __init__(self, credits: list[dict]) -> None:
        self._pages = [{"credits": credits}]

    def retrieve(self) -> RetrievalOutcome:
        return RetrievalOutcome(
            raw_pages=copy.deepcopy(self._pages), credits=parse_credits(self._pages)
        )


class FakeStore:
    def write(self, snapshot: Snapshot) -> str:
        key = f"snapshots/{snapshot.taken_at.strftime('%Y%m%dT%H%M%SZ')}.json"
        snapshot.key = key
        return key

    def read_latest_prior(self, before_key: str | None = None) -> Snapshot | None:
        return None


class FakeCategoryRetriever:
    """Returns the bound category in a ``list-cost-category-definitions`` page."""

    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def retrieve(self) -> CategoryRetrievalOutcome:
        return CategoryRetrievalOutcome(
            raw_pages=copy.deepcopy(self._pages),
            categories=parse_cost_categories(self._pages),
        )


class FakeDefinitionRetriever:
    messages: list[str] = []

    def __init__(self) -> None:
        payload = _load("describe_cost_category_definition.json")["CostCategory"]
        self._definition = CostCategoryDefinition(
            arn=payload["CostCategoryArn"], name=payload["Name"], rules=payload["Rules"]
        )

    def describe(self, cost_category_arn: str):
        return self._definition if cost_category_arn == self._definition.arn else None


class _Resolution:
    def __init__(self, names: dict[str, str | None]) -> None:
        self.names = names


class FakeResolver:
    consulted = True
    unavailable = False

    def resolve(self, account_ids: list[str]):
        return _Resolution({"444444444444": "Platform", "555555555555": "DataScience"})


def _category_pages() -> list[dict]:
    payload = _load("describe_cost_category_definition.json")["CostCategory"]
    return [
        {
            "CostCategoryReferences": [
                {
                    "CostCategoryArn": payload["CostCategoryArn"],
                    "Name": payload["Name"],
                    "EffectiveStart": payload["EffectiveStart"],
                }
            ]
        }
    ]


def _with_id(name: str, credit_id: str) -> dict:
    credit = _load(name)["credits"][0]
    credit["creditId"] = credit_id
    return credit


def _live_five() -> list[dict]:
    """Five captured credits: bound (resolved), bound-paused, unbound, and two live-shape."""
    live = _load("get_credits_live_shape.json")["credits"]
    return [
        _with_id("get_credits_cc_bound_active.json", "19000000003"),
        _with_id("get_credits_cc_bound_paused.json", "19000000004"),
        _with_id("get_credits_cc_unbound_active.json", "19000000005"),
        *live,
    ]


def _run_with_sns(credits: list[dict], **orchestrator_kwargs) -> tuple[RunResult, list[str]]:
    """Run the orchestrator publishing through a real notifier to moto SNS."""
    with mock_aws():
        sns = boto3.client("sns", region_name=REGION)
        topic_arn = sns.create_topic(Name="aws-credits-reporter-report")["TopicArn"]
        orchestrator = Orchestrator(
            credit_retriever=FakeRetriever(credits),
            snapshot_store=FakeStore(),
            now=lambda: NOW,
            notifier=Notifier(sns, topic_arn, sleep=lambda _: None),
            **orchestrator_kwargs,
        )
        result = orchestrator.run()
        topic = sns_backends[DEFAULT_ACCOUNT_ID][REGION].topics[topic_arn]
        messages = [notification[1] for notification in topic.sent_notifications]
    return result, messages


# --------------------------------------------------------------------------
# A partial-binding credit reaches the published body
# --------------------------------------------------------------------------


def test_partial_binding_credit_appears_in_the_published_sns_body(aws_env):
    credits = _live_five()
    partial = credits[2]  # the unbound-active credit gains an ARN but no rule
    partial["costCategoryArn"] = _category_pages()[0]["CostCategoryReferences"][0][
        "CostCategoryArn"
    ]

    result, messages = _run_with_sns(
        credits,
        category_retriever=FakeCategoryRetriever(_category_pages()),
        definition_retriever=FakeDefinitionRetriever(),
        account_name_resolver=FakeResolver(),
    )

    assert result.success, result.error_message
    assert [item.credit_id for item in result.attention] == ["19000000005"]
    assert result.attention[0].reasons == [REASON_PARTIAL_BINDING]

    assert len(messages) == 1
    body = messages[0]
    assert body.count(NEEDS_ATTENTION_HEADING) == 1
    assert f"- 19000000005: {REASON_PARTIAL_BINDING}" in body
    assert body.index(SHARING_SUMMARY_HEADING) < body.index(NEEDS_ATTENTION_HEADING)
    # The attention block never alters the exit status (Req 26.6).
    assert result.delivery_failed is False


# --------------------------------------------------------------------------
# Everything resolved: today's email is unchanged
# --------------------------------------------------------------------------


def test_fully_resolved_live_scenario_has_no_attention_block(aws_env):
    result, messages = _run_with_sns(
        _live_five(),
        category_retriever=FakeCategoryRetriever(_category_pages()),
        definition_retriever=FakeDefinitionRetriever(),
        account_name_resolver=FakeResolver(),
    )

    assert result.success, result.error_message
    assert result.total_count == 5
    assert result.attention == []
    # The bound credit really resolved: Name, rule and named members.
    bound = {g.credit.creditId: g for g in result.sharing.groups}["19000000003"]
    assert bound.cost_category_name == "CreditGroup"
    assert bound.rule_name == "Credit_Rule"
    assert [m.account_name for m in bound.member_accounts] == ["Platform", "DataScience"]

    assert len(messages) == 1
    body = messages[0]
    assert NEEDS_ATTENTION_HEADING not in body
    # Byte-for-byte what the body builder produces without the parameter.
    assert body == build_dashboard_body(
        result.dashboard,
        result.expiration,
        result.new_credits,
        result.inactive_credits,
        result.sharing,
    )
    # No extra blank lines: the sharing block is followed by exactly one blank line.
    assert "\n\n\n" not in body


def test_unresolved_bound_credit_is_listed_once_it_loses_its_names(aws_env):
    # Same five credits, but no category retriever and no definition adapters:
    # the bound credit's Name is unresolved and its members are unavailable.
    result, messages = _run_with_sns(_live_five())

    assert result.success
    assert [item.credit_id for item in result.attention] == ["19000000003"]
    body = messages[0]
    assert "- 19000000003: cost category name could not be resolved" in body
    assert "cost category definition could not be retrieved" in body
