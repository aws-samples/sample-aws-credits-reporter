"""Phase 5 checkpoint (task 20): the pipeline wired into ``Orchestrator.run()``.

Drives the full orchestrator once, end to end, against the recorded fixtures
with no real AWS: the Cost Explorer call is a botocore ``Stubber`` fed from
``list_cost_category_definitions.json`` and wrapped in a real
``CategoryRetriever``; credits and the snapshot store are in-memory fakes.

The single run proves the five phases are integrated:

* Phase 1 - credits are retrieved and a ``Snapshot`` is written.
* Phase 3 - the notification body reaches the injected notifier.
* Phase 4 - the Lifecycle_Analyzer splits Active/Inactive and the dashboard
  and new-credit detection run (credit-0002 is Exhausted/Inactive).
* Phase 5 - cost category definitions are retrieved and ride in the snapshot;
  the ``SharingResult`` reaches the dashboard (sharing figures) and the email
  body.

No real payer account is touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.stub import Stubber

from aws_credits_reporter.category_retriever import CategoryRetriever, make_cost_explorer_client
from aws_credits_reporter.credit_retriever import RetrievalOutcome
from aws_credits_reporter.models import Snapshot, parse_cost_categories, parse_credits
from aws_credits_reporter.notifier import NotifyResult
from aws_credits_reporter.report_body import DASHBOARD_SUMMARY_HEADING, EXPIRED_CREDITS_HEADING, INACTIVE_CREDITS_HEADING, SHARING_SUMMARY_HEADING
from aws_credits_reporter.orchestrator import Orchestrator
from aws_credits_reporter.sharing_classifier import (
    COST_CATEGORY_BASED,
    SHARED_BY_DEFAULT,
)

FIXTURES = Path(__file__).parent / "fixtures"
# A snapshot date after both credits' endDates so credit-0001 (endDate
# 2025-01-14) is Expired-by-date is avoided: pick a date on which credit-0001
# is still Active (endDate in the future) and credit-0002 is Exhausted
# (remaining 0.00).
NOW = datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeRetriever:
    def __init__(self, page: dict) -> None:
        self._pages = [page]

    def retrieve(self) -> RetrievalOutcome:
        return RetrievalOutcome(
            raw_pages=list(self._pages), credits=parse_credits(self._pages)
        )


class FakeStore:
    """In-memory store that records the written snapshot; no prior history."""

    def __init__(self) -> None:
        self.written: Snapshot | None = None

    def write(self, snapshot: Snapshot) -> str:
        key = f"snapshots/{snapshot.taken_at.strftime('%Y%m%dT%H%M%SZ')}.json"
        snapshot.key = key
        self.written = snapshot
        return key

    def read_latest_prior(self, before_key: str | None = None) -> Snapshot | None:
        return None


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send(self, body: str) -> NotifyResult:
        self.calls.append(body)
        return NotifyResult(sent=True, attempts=1, messages=["notification sent"])


def _stubbed_category_retriever() -> tuple[CategoryRetriever, Stubber]:
    """A real ``CategoryRetriever`` over a stubbed ``ce`` client (no AWS)."""
    ce = make_cost_explorer_client(session=boto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # pragma: allowlist secret
        region_name="us-east-1",
    ))
    stubber = Stubber(ce)
    stubber.add_response(
        "list_cost_category_definitions",
        _load("list_cost_category_definitions.json"),
    )
    return CategoryRetriever(ce, sleep=lambda _: None), stubber


def test_pipeline_is_wired_end_to_end():
    page1 = _load("get_credits_page1.json")
    store = FakeStore()
    notifier = RecordingNotifier()
    category_retriever, stubber = _stubbed_category_retriever()

    with stubber:
        orchestrator = Orchestrator(
            credit_retriever=FakeRetriever(page1),
            snapshot_store=store,
            now=lambda: NOW,
            notifier=notifier,
            category_retriever=category_retriever,
        )
        result = orchestrator.run()
        stubber.assert_no_pending_responses()

    # -- Phase 1: retrieval + snapshot ------------------------------------
    assert result.success, result.error_message
    assert result.total_count == 2
    assert result.snapshot_key is not None
    assert store.written is not None

    # -- Phase 5: cost categories parsed from the snapshot ----------------
    # The unmodified list-cost-category-definitions pages ride in the same
    # snapshot object alongside get-credits (Req 13.7).
    assert store.written.raw_cost_categories == [
        _load("list_cost_category_definitions.json")
    ]
    categories = parse_cost_categories(store.written.raw_cost_categories)
    assert [c.Name for c in categories] == [
        "CreditSharingGroups",
        "BusinessUnits",
        None,
    ]

    # -- Phase 4: lifecycle split applied ---------------------------------
    # credit-0001 is Active (endDate 2025-01-14 is after the snapshot date);
    # credit-0002 has remaining 0.00, so it is Exhausted -> Inactive.
    assert result.snapshot_date == NOW.date()
    assert [c.creditId for c in result.inactive_credits] == ["credit-0002"]
    assert result.dashboard is not None
    assert result.dashboard.active_count == 1
    assert result.dashboard.inactive_count == 1
    # Only Active_Credits are tier candidates (Req 7.9): credit-0002 is never
    # tiered even though its endDate is in the past.
    assert "credit-0002" not in result.expiration.this_month.credit_ids
    assert "credit-0002" not in result.expiration.next_three_months.credit_ids

    # -- Phase 5: SharingResult reaches the run result --------------------
    assert result.sharing is not None
    by_id = {g.credit.creditId: g for g in result.sharing.groups}
    # credit-0001 has shareableAccounts -> Shared by default (Req 18.1).
    assert by_id["credit-0001"].sharing_option == SHARED_BY_DEFAULT
    # credit-0002 has no shareableAccounts and no costCategoryArn/ruleName ->
    # Shared by default; the authoritative ARN+rule pair is the sole
    # cost-category signal (Req 18.4).
    assert by_id["credit-0002"].sharing_option == SHARED_BY_DEFAULT
    # -- Phase 5 + 6: dashboard sharing figures count Active_Credits only --
    # (Req 22.7). credit-0001 is Active and Shared by default; credit-0002 is
    # Exhausted/Inactive, so it never contributes to any group.
    default = result.dashboard.sharing_by_option[SHARED_BY_DEFAULT]
    cost_cat = result.dashboard.sharing_by_option[COST_CATEGORY_BASED]
    assert default.count == 1
    assert cost_cat.count == 0
    # -- Phase 3 + 5: notification body carries dashboard + sharing -------
    assert len(notifier.calls) == 1
    body = notifier.calls[0]
    assert DASHBOARD_SUMMARY_HEADING in body
    assert SHARING_SUMMARY_HEADING in body
    # credit-0002's endDate is in the past, so the only inactive credit is
    # Expired and the trailing listing is titled "EXPIRED CREDITS" (Req 22.8).
    assert EXPIRED_CREDITS_HEADING in body
    assert INACTIVE_CREDITS_HEADING not in body
    assert "- credit-0002" in body  # the inactive credit is listed as a bullet
    assert "notification sent" in result.messages
