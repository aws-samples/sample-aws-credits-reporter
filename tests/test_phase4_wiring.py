"""Task 14.1: analysis wired into ``Orchestrator.run()``.

Drives the orchestrator with an in-memory snapshot store that keeps real
history (write, ``read_latest_prior``) so a second run can be compared
against the first. A raising prior read is switchable per test (Req 6.7).
No AWS client is constructed anywhere in this module.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aws_credits_reporter.cli import main
from aws_credits_reporter.credit_retriever import RetrievalOutcome
from aws_credits_reporter.dashboard import BASELINE_NOTICE, COMPARISON_ERROR_NOTICE
from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
from aws_credits_reporter.models import Snapshot, parse_credits
from aws_credits_reporter.new_credit_detector import (
    BASELINE_MESSAGE,
    COMPARISON_ERROR_MESSAGE,
    ZERO_NEW_MESSAGE,
)
from aws_credits_reporter.notifier import NotifyResult
from aws_credits_reporter.report_body import OVERVIEW_HEADING, DASHBOARD_SUMMARY_HEADING, build_dashboard_body
from aws_credits_reporter.orchestrator import PRIOR_READ_FAILED_PREFIX, Orchestrator
from aws_credits_reporter.snapshot_store import SnapshotReadError

FIXTURES = Path(__file__).parent / "fixtures"
PREFIX = "snapshots/"
T0 = datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _page1() -> dict:
    return _load("get_credits_page1.json")


def _page1_plus_page2() -> dict:
    """One page holding the union of both fixtures: credit-0003 is the new one."""
    return {"credits": _page1()["credits"] + _load("get_credits_page2.json")["credits"]}


class FakeRetriever:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def retrieve(self) -> RetrievalOutcome:
        return RetrievalOutcome(raw_pages=list(self._pages), credits=parse_credits(self._pages))


class HistoryStore:
    """In-memory snapshot store with the same read semantics as ``SnapshotStore``.

    ``objects`` maps key to ``Snapshot`` (or to ``None`` to model an object
    that exists in the listing but cannot be read). ``fail_prior_read`` makes
    ``read_latest_prior`` raise.
    """

    def __init__(self) -> None:
        self.objects: dict[str, Snapshot | None] = {}
        self.fail_prior_read = False
        self.prior_read_calls: list[str | None] = []
        self.write_order: list[str] = []

    def write(self, snapshot: Snapshot) -> str:
        key = f"{PREFIX}{snapshot.taken_at.strftime('%Y%m%dT%H%M%SZ')}.json"
        assert key not in self.objects, "the fake never overwrites"
        snapshot.key = key
        self.objects[key] = snapshot
        self.write_order.append(key)
        return key

    def add_unreadable(self, taken_at: datetime) -> str:
        key = f"{PREFIX}{taken_at.strftime('%Y%m%dT%H%M%SZ')}.json"
        self.objects[key] = None
        return key

    def _keys(self) -> list[str]:
        return sorted(self.objects)

    def read_latest_prior(self, before_key: str | None = None) -> Snapshot | None:
        self.prior_read_calls.append(before_key)
        if self.fail_prior_read:
            raise SnapshotReadError(before_key or PREFIX, "get_object failed: AccessDenied")
        keys = [k for k in self._keys() if before_key is None or k < before_key]
        if not keys:
            return None
        snapshot = self.objects[keys[-1]]
        if snapshot is None:
            raise SnapshotReadError(keys[-1], "could not decode snapshot")
        return snapshot


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send(self, body: str) -> NotifyResult:
        self.calls.append(body)
        return NotifyResult(sent=True, attempts=1, messages=["notification sent"])


class Clock:
    """Returns ``start`` then advances by one day per call, so keys never collide."""

    def __init__(self, start: datetime = T0) -> None:
        self._next = start

    def __call__(self) -> datetime:
        value = self._next
        self._next = value + timedelta(days=1)
        return value


def _orchestrator(pages: list[dict], store: HistoryStore, clock: Clock, notifier=None) -> Orchestrator:
    return Orchestrator(
        credit_retriever=FakeRetriever(pages),
        snapshot_store=store,
        now=clock,
        notifier=notifier,
    )


# --------------------------------------------------------------------------
# New-credit detection (Req 6.1, 6.2, 6.5, 6.6, 6.7)
# --------------------------------------------------------------------------


def test_first_run_is_initial_baseline():
    store, clock = HistoryStore(), Clock()
    result = _orchestrator([_page1()], store, clock).run()

    assert result.success, result.error_message
    assert result.new_credits is not None
    assert result.new_credits.is_initial_baseline
    assert result.new_credits.new_credit_ids == set()
    assert result.prior_snapshot_key is None
    assert BASELINE_MESSAGE in result.messages
    assert BASELINE_NOTICE in result.dashboard.notices
    # The prior read was issued for the just-written key (Req 6.1).
    assert store.prior_read_calls == [result.snapshot_key]


def test_second_run_detects_new_credit_against_prior_snapshot():
    store, clock = HistoryStore(), Clock()
    first = _orchestrator([_page1()], store, clock).run()
    second = _orchestrator([_page1_plus_page2()], store, clock).run()

    assert second.success, second.error_message
    assert second.prior_snapshot_key == first.snapshot_key
    assert second.new_credits.new_credit_ids == {"credit-0003"}
    assert not second.new_credits.is_initial_baseline
    assert not second.new_credits.comparison_error
    assert any("1 credit newly issued" in m and "credit-0003" in m for m in second.messages)
    assert BASELINE_MESSAGE not in second.messages
    # The dashboard identifies the new credit by creditId (Req 6.3).
    assert second.dashboard.new_credit_ids == ["credit-0003"]


def test_identical_second_run_reports_zero_new_credits():
    store, clock = HistoryStore(), Clock()
    _orchestrator([_page1()], store, clock).run()
    second = _orchestrator([_page1()], store, clock).run()

    assert second.success
    assert second.new_credits.new_credit_ids == set()
    assert ZERO_NEW_MESSAGE in second.messages  # Req 6.5


def test_prior_read_failure_is_comparison_error_and_run_still_succeeds():
    store, clock = HistoryStore(), Clock()
    _orchestrator([_page1()], store, clock).run()
    store.fail_prior_read = True
    second = _orchestrator([_page1_plus_page2()], store, clock).run()

    assert second.success, second.error_message
    assert second.new_credits.comparison_error
    assert second.new_credits.new_credit_ids == set()  # nothing classified as new (Req 6.7)
    assert second.prior_snapshot_key is None
    assert any(m.startswith(PRIOR_READ_FAILED_PREFIX) and "AccessDenied" in m for m in second.messages)
    assert COMPARISON_ERROR_MESSAGE in second.messages
    assert COMPARISON_ERROR_NOTICE in second.dashboard.notices
    assert second.dashboard.new_credit_ids == []


def test_unreadable_prior_object_is_comparison_error():
    """The store raising on the chosen prior object (not only on listing) is Req 6.7 too."""
    store, clock = HistoryStore(), Clock()
    store.add_unreadable(clock())  # exists in the listing, cannot be decoded
    result = _orchestrator([_page1()], store, clock).run()

    assert result.success
    assert result.new_credits.comparison_error
    assert not result.new_credits.is_initial_baseline
    assert COMPARISON_ERROR_MESSAGE in result.messages


# --------------------------------------------------------------------------
# Expiration analysis (Req 7)
# --------------------------------------------------------------------------


def test_expiration_tiers_are_relative_to_snapshot_time():
    # page1 endDates: credit-0001 2025-01-14, credit-0002 2024-05-31.
    # With the clock at 2024-05-01, credit-0001 falls outside the three-month
    # window (2024-05 to 2024-08). credit-0002 has remainingAmount 0.00, so
    # under Req 4.7 its Lifecycle_Status is Exhausted and Req 7.3 keeps it
    # out of every tier. Both tiers therefore end up empty.
    store = HistoryStore()
    result = _orchestrator([_page1()], store, Clock(datetime(2024, 5, 1, tzinfo=timezone.utc))).run()

    assert result.success
    assert result.expiration.this_month.credit_ids == []
    assert result.expiration.next_three_months.credit_ids == []
    assert result.expiration.window == ExpirationAnalyzer(
        datetime(2024, 5, 1, tzinfo=timezone.utc)
    ).window
    assert "expiring this month: 0 credits, total remaining 0.00" in result.messages
    assert "expiring in the next three months: 0 credits, total remaining 0.00" in result.messages
    assert result.dashboard.this_month_count == 0


def test_injected_analyzer_factory_receives_snapshot_time():
    seen: list[datetime] = []

    def factory(now: datetime) -> ExpirationAnalyzer:
        seen.append(now)
        return ExpirationAnalyzer(now)

    orchestrator = Orchestrator(
        credit_retriever=FakeRetriever([_page1()]),
        snapshot_store=HistoryStore(),
        now=Clock(),
        analyzer_factory=factory,
    )
    result = orchestrator.run()

    assert result.success
    assert seen == [T0]


# --------------------------------------------------------------------------
# Email body (Req 11.1)
# --------------------------------------------------------------------------


def test_notifier_receives_dashboard_body():
    store, clock = HistoryStore(), Clock()
    _orchestrator([_page1()], store, clock).run()
    notifier = RecordingNotifier()
    result = _orchestrator([_page1_plus_page2()], store, clock, notifier).run()

    assert len(notifier.calls) == 1
    body = notifier.calls[0]
    assert body == build_dashboard_body(
        result.dashboard, result.expiration, result.new_credits, result.inactive_credits
    )
    assert DASHBOARD_SUMMARY_HEADING in body
    assert OVERVIEW_HEADING in body  # a new credit was found
    assert "credit-0003" in body
    assert body.index(OVERVIEW_HEADING) < body.index(DASHBOARD_SUMMARY_HEADING)  # Req 11.2
    assert "notification sent" in result.messages


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_prints_dashboard_line_and_analysis_messages(capsys):
    store, clock = HistoryStore(), Clock()
    _orchestrator([_page1()], store, clock).run()

    exit_code = main(
        ["--bucket", "unused"],
        orchestrator_factory=lambda args: _orchestrator([_page1_plus_page2()], store, clock),
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Dashboard: 2 active (total remaining $8485.06)" in out
    assert "1 newly issued" in out
    assert "credit-0003" in out


def test_cli_prints_baseline_message_on_first_run(capsys):
    exit_code = main(
        ["--bucket", "unused"],
        orchestrator_factory=lambda args: _orchestrator([_page1()], HistoryStore(), Clock()),
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert BASELINE_MESSAGE in out
    assert "0 newly issued" in out


@pytest.mark.parametrize("fail_prior_read", [False, True])
def test_run_result_analysis_fields_are_populated_on_success(fail_prior_read):
    store, clock = HistoryStore(), Clock()
    store.fail_prior_read = fail_prior_read
    result = _orchestrator([_page1()], store, clock).run()

    assert result.success
    for name in ("new_credits", "expiration", "dashboard"):
        assert getattr(result, name) is not None, name
