"""Task 4.3: unit tests for the Orchestrator and the CLI (Phase 1 scope).

These tests exercise the Phase 1 contract of ``Orchestrator.run()`` and
``aws_credits_reporter.cli.run``/``main`` with plain in-memory stubs injected into
the orchestrator. No boto3 client, botocore stubber, or moto is used here:
the retriever and snapshot store are hand-written fakes, so the tests stay
fast and focused on the seams the design calls out (the orchestrator accepts
injected clients; the CLI accepts an injected orchestrator).

Covered:

- A successful run prints one line per credit and the totals and exits 0
  (Req 3.1, 3.4, 3.5).
- A zero-credit run prints the zero-credits message and exits 0 (Req 3.6).
- A retrieval failure prints the recorded error to stderr and exits non-zero
  (Req 3.1, 3.7).
- A snapshot write failure prints the recorded error to stderr and exits
  non-zero (Req 3.1, 3.7).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from aws_credits_reporter import cli
from aws_credits_reporter.credit_retriever import (
    AuthorizationError,
    RetrievalError,
    RetrievalOutcome,
    REQUIRED_PERMISSION,
)
from aws_credits_reporter.models import Credit, CurrencyAmount, Snapshot
from aws_credits_reporter.orchestrator import Orchestrator
from aws_credits_reporter.snapshot_store import SnapshotWriteError

T0 = datetime(2024, 6, 15, 8, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# In-memory stubs (no AWS)
# --------------------------------------------------------------------------


def _credit(
    credit_id: str,
    *,
    account_id: str = "111111111111",
    status: str = "ENABLED",
    remaining: str | None = "100.00",
    currency: str = "USD",
    end_date: date | None = None,
) -> Credit:
    amount = (
        CurrencyAmount(amount=Decimal(remaining), currency=currency)
        if remaining is not None
        else None
    )
    return Credit(
        creditId=credit_id,
        accountId=account_id,
        creditStatus=status,
        remainingAmount=amount,
        endDate=end_date,
    )


class StubRetriever:
    """Returns a fixed outcome, or raises a fixed error to model a failure."""

    def __init__(
        self,
        credits: list[Credit] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._credits = credits or []
        self._error = error

    def retrieve(self) -> RetrievalOutcome:
        if self._error is not None:
            raise self._error
        # The raw page mirrors what the parsed credits came from; the
        # orchestrator only writes it into the snapshot, it does not re-parse.
        raw = {"credits": [{"creditId": c.creditId} for c in self._credits]}
        return RetrievalOutcome(raw_pages=[raw], credits=list(self._credits))


class StubStore:
    """Assigns a deterministic key on write, or raises to model a write failure.

    ``read_latest_prior`` returns the empty baseline so the analysis wired
    into ``run()`` stays on its no-prior-snapshot path and the Phase 1
    assertions are unaffected.
    """

    def __init__(self, *, write_error: Exception | None = None) -> None:
        self._write_error = write_error
        self.written: list[Snapshot] = []

    def write(self, snapshot: Snapshot) -> str:
        if self._write_error is not None:
            raise self._write_error
        key = f"snapshots/{snapshot.taken_at.strftime('%Y%m%dT%H%M%SZ')}.json"
        snapshot.key = key
        self.written.append(snapshot)
        return key

    def read_latest_prior(self, before_key: str | None = None) -> Snapshot | None:
        return None



def _orchestrator(retriever: StubRetriever, store: StubStore) -> Orchestrator:
    return Orchestrator(
        credit_retriever=retriever,
        snapshot_store=store,
        now=lambda: T0,
    )


def _args(**overrides) -> argparse.Namespace:
    """A minimal argument namespace (no notification)."""
    values = {
        "bucket": "unused",
        "prefix": "snapshots/",
        "sns_topic_arn": None,
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


# --------------------------------------------------------------------------
# Orchestrator: successful run
# --------------------------------------------------------------------------


def test_orchestrator_success_summarizes_credits_and_snapshot():
    credits = [
        _credit("credit-0001", remaining="100.00"),
        _credit("credit-0002", remaining="50.50"),
    ]
    store = StubStore()
    result = _orchestrator(StubRetriever(credits), store).run()

    assert result.success, result.error_message
    assert result.error_message is None
    assert [c.creditId for c in result.credits] == ["credit-0001", "credit-0002"]
    assert result.total_count == 2
    assert result.total_remaining == Decimal("150.50")
    assert result.remaining_currency == "USD"
    assert result.snapshot_key == "snapshots/20240615T080000Z.json"
    # The unmodified raw page was handed to the store for persistence.
    assert store.written[0].raw_get_credits == [
        {"credits": [{"creditId": "credit-0001"}, {"creditId": "credit-0002"}]}
    ]


def test_orchestrator_zero_credits_still_succeeds_and_writes_snapshot():
    store = StubStore()
    result = _orchestrator(StubRetriever([]), store).run()

    assert result.success, result.error_message
    assert result.credits == []
    assert result.total_count == 0
    assert result.total_remaining == Decimal("0")
    assert result.snapshot_key is not None
    assert len(store.written) == 1


def test_orchestrator_retrieval_failure_is_captured_not_raised():
    error = AuthorizationError(
        f"not authorized to call GetCredits (missing {REQUIRED_PERMISSION})"
    )
    store = StubStore()
    result = _orchestrator(StubRetriever(error=error), store).run()

    assert not result.success
    assert result.error_message is not None
    assert REQUIRED_PERMISSION in result.error_message
    # Nothing was persisted when retrieval failed.
    assert store.written == []
    assert result.snapshot_key is None


def test_orchestrator_snapshot_write_failure_is_captured_not_raised():
    write_error = SnapshotWriteError(
        "snapshots/20240615T080000Z.json", attempts=3, last_error="AccessDenied"
    )
    result = _orchestrator(
        StubRetriever([_credit("credit-0001")]),
        StubStore(write_error=write_error),
    ).run()

    assert not result.success
    assert result.error_message is not None
    assert "AccessDenied" in result.error_message
    # Retrieval succeeded, so the credits are still available for context.
    assert result.total_count == 1


# --------------------------------------------------------------------------
# CLI: successful run
# --------------------------------------------------------------------------


def test_cli_success_prints_per_credit_lines_and_totals_and_exits_zero(capsys):
    credits = [
        _credit("credit-0001", account_id="111111111111", remaining="100.00",
                end_date=date(2025, 1, 31)),
        _credit("credit-0002", account_id="222222222222", remaining="50.50"),
    ]
    store = StubStore()
    exit_code = cli.run(_args(), _orchestrator(StubRetriever(credits), store))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_SUCCESS

    lines = out.splitlines()
    # One line per credit, starting with the creditId (Req 3.4).
    assert any(line.startswith("credit-0001 111111111111 ENABLED ") for line in lines)
    assert any(line.startswith("credit-0002 222222222222 ENABLED ") for line in lines)
    # The per-credit line carries remaining amount and endDate (Req 3.4).
    credit1_line = next(line for line in lines if line.startswith("credit-0001 "))
    assert "100.00 USD" in credit1_line
    assert "2025-01-31" in credit1_line
    credit2_line = next(line for line in lines if line.startswith("credit-0002 "))
    assert cli.NOT_AVAILABLE in credit2_line  # no endDate

    # The totals (Req 3.5).
    assert "Total credits: 2" in out
    assert "Total remaining: 150.50 USD" in out
    assert "Snapshot written: snapshots/20240615T080000Z.json" in out


def test_cli_zero_credits_prints_zero_message_and_exits_zero(capsys):
    exit_code = cli.run(_args(), _orchestrator(StubRetriever([]), StubStore()))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_SUCCESS
    assert "Zero credits were retrieved." in out
    assert "Total credits: 0" in out


# --------------------------------------------------------------------------
# CLI: failures print to stderr and exit non-zero (Req 3.7)
# --------------------------------------------------------------------------


def test_cli_retrieval_failure_prints_error_to_stderr_and_exits_nonzero(capsys):
    error = RetrievalError("get-credits failed after 5 attempts: InternalServerException")
    exit_code = cli.run(
        _args(), _orchestrator(StubRetriever(error=error), StubStore())
    )

    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAILURE
    assert "InternalServerException" in captured.err
    assert captured.err.startswith("error:")
    # The error goes to stderr, not stdout.
    assert "InternalServerException" not in captured.out


def test_cli_snapshot_write_failure_prints_error_to_stderr_and_exits_nonzero(capsys):
    write_error = SnapshotWriteError(
        "snapshots/20240615T080000Z.json", attempts=3, last_error="AccessDenied"
    )
    exit_code = cli.run(
        _args(),
        _orchestrator(StubRetriever([_credit("credit-0001")]), StubStore(write_error=write_error)),
    )

    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAILURE
    assert "AccessDenied" in captured.err
    assert "failed to write snapshot" in captured.err


def test_cli_failure_without_recorded_message_still_exits_nonzero(capsys):
    class BlankFailureOrchestrator:
        def run(self):
            from aws_credits_reporter.orchestrator import RunResult

            return RunResult(success=False, error_message=None)

    exit_code = cli.run(_args(), BlankFailureOrchestrator())

    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAILURE
    assert "run failed without a recorded error message" in captured.err


# --------------------------------------------------------------------------
# CLI: main() wires the injected orchestrator factory (Req 3.1)
# --------------------------------------------------------------------------


def test_main_uses_injected_orchestrator_factory_and_returns_exit_code(capsys):
    store = StubStore()
    factory = lambda args: _orchestrator(StubRetriever([_credit("credit-0001")]), store)

    exit_code = cli.main(["--bucket", "unused"], orchestrator_factory=factory)

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_SUCCESS
    assert out.count("credit-0001") >= 1
    assert "Total credits: 1" in out


def test_main_returns_failure_when_factory_raises_setup_error(capsys):
    def factory(args):
        raise cli.SetupError("could not resolve the caller identity via STS: boom")

    exit_code = cli.main(["--bucket", "unused"], orchestrator_factory=factory)

    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAILURE
    assert "could not resolve the caller identity" in captured.err
