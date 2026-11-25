"""CLI_Runner: on-demand command-line entry point for AWS Credits Reporter.

Run:

- Parse ``--bucket`` (required), ``--prefix``, ``--profile``, ``--region``.
- Build real boto3 clients from a session honoring ``--profile``: Billing is
  always pinned to ``us-east-1`` (as are STS, Cost Explorer, and Organizations);
  ``--region`` only affects the S3 and SNS clients.
- Resolve the caller's account id via STS (``GetCredits`` requires it), wire
  the components through ``wiring.build_orchestrator``, and invoke
  ``Orchestrator.run()`` exactly once.
- On success print one line per credit, the totals, the snapshot key, and any
  recorded messages to stdout and return ``0``.
- On failure print the recorded error message to stderr and return ``1``.

Notification:

- ``--sns-topic-arn`` and ``--dry-run`` configure the Notifier. SNS is the
  only delivery backend and the email body is the only deliverable.
- Notification is *opt-in*: it runs only when at least one of those options
  is given. Without them the run prints a "Notification skipped" line.
  ``--dry-run`` without ``--sns-topic-arn`` records a
  configuration error instead of silently skipping.
- ``--dry-run`` prepares the notification (so a configuration error still
  shows up) but never calls SNS and needs no SNS client or permissions.
  Retrieval and the snapshot write are still performed.
- Notification outcomes never change the exit code: the ``NotifyResult``
  messages are printed to stdout; a delivery failure is additionally
  printed to stderr as a warning while the exit code stays ``0``.

After the totals a one-line ``Dashboard:`` summary is printed (active count
and total, expiring counts, newly issued count). The baseline,
comparison-error, new-credit, and expiration tier messages flow through
``RunResult.messages`` like every other note.

``main()`` is the console-script target. ``run(args, orchestrator)`` is the
seam tests use to inject a fake orchestrator; ``build_orchestrator`` maps the
parsed arguments onto ``wiring.build_orchestrator`` (the factory tests replace
to avoid touching AWS); ``build_notifier`` maps them to a ``Notifier`` given a
client factory.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Protocol, Sequence, TextIO

from aws_credits_reporter import wiring
from aws_credits_reporter.credit_retriever import BILLING_REGION
from aws_credits_reporter.dashboard import Dashboard
from aws_credits_reporter.models import Credit, CurrencyAmount, lifecycle_status
from aws_credits_reporter.notifier import Notifier
from aws_credits_reporter.orchestrator import Orchestrator, RunResult
from aws_credits_reporter.wiring import DEFAULT_PREFIX, SetupError

NOT_AVAILABLE = "not available"
EXIT_SUCCESS = 0
EXIT_FAILURE = 1

NOTIFICATION_SKIPPED_MESSAGE = (
    "Notification skipped: no notification options given "
    "(pass --sns-topic-arn to enable it)."
)
# Every option in this set counts as an explicit request to notify.
NOTIFICATION_OPTIONS = ("sns_topic_arn", "dry_run")


class _OrchestratorLike(Protocol):
    def run(self) -> RunResult: ...


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aws-credits-reporter",
        description=(
            "Retrieve all AWS Organization credits for the payer account, "
            "persist a raw snapshot to S3, and print a listing."
        ),
    )
    parser.add_argument(
        "--bucket",
        required=True,
        help="S3 bucket that receives the snapshot object (required).",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"S3 key prefix for snapshots, used as is, so end it with '/' (default: {DEFAULT_PREFIX}).",
    )
    parser.add_argument(
        "--expected-bucket-owner",
        default=None,
        help=(
            "Account id that must own the snapshot bucket (default: the caller's "
            "own account). Sent on every S3 call so a bucket name owned by another "
            "account is refused."
        ),
    )
    parser.add_argument(
        "--expected-topic-owner",
        default=None,
        help=(
            "Account id that must own the --sns-topic-arn topic (default: the "
            "caller's own account). A topic in another account is refused."
        ),
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS CLI profile name to use for credentials.",
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(
            "AWS region for the S3 and SNS clients. The Billing API is always called "
            f"in {BILLING_REGION} regardless of this value."
        ),
    )

    notify = parser.add_argument_group(
        "notification",
        description=(
            "SNS delivery of the email body. Notification is opt-in: it runs "
            "only when at least one of these options is given; otherwise the run "
            "prints 'Notification skipped'. Giving --dry-run without "
            "--sns-topic-arn is an explicit request, so the missing topic is "
            "reported as a configuration error. Notification problems never "
            "change the exit code."
        ),
    )
    notify.add_argument(
        "--sns-topic-arn",
        default=None,
        help="SNS topic ARN to publish the email body to.",
    )
    notify.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Prepare the notification but do not call SNS (no SNS client is "
            "built). Retrieval and the snapshot write still run."
        ),
    )
    return parser


def notification_requested(args: argparse.Namespace) -> bool:
    """True when any notification option was given on the command line."""
    return any(getattr(args, name, None) for name in NOTIFICATION_OPTIONS)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# Client / orchestrator factory (monkeypatch target for tests)
# --------------------------------------------------------------------------


def build_orchestrator(args: argparse.Namespace) -> Orchestrator:
    """Wire the real orchestrator from the parsed arguments (see ``wiring``).

    Raises ``SetupError`` when credentials are missing or STS rejects the
    caller, so ``main`` can print a clean message instead of a traceback.
    """
    return wiring.build_orchestrator(
        bucket=args.bucket,
        prefix=args.prefix,
        profile=args.profile,
        region=args.region,
        sns_topic_arn=args.sns_topic_arn,
        dry_run=args.dry_run,
        expected_bucket_owner=args.expected_bucket_owner,
        expected_topic_owner=args.expected_topic_owner,
    )


def build_notifier(
    args: argparse.Namespace,
    client_factory: Callable[[str], Any],
    sleep: Callable[[float], Any] = time.sleep,
) -> Notifier | None:
    """Map the notification options to a ``Notifier``; ``None`` when none were given."""
    return wiring.build_notifier(
        getattr(args, "sns_topic_arn", None),
        bool(getattr(args, "dry_run", False)),
        client_factory,
        sleep=sleep,
    )


# --------------------------------------------------------------------------
# Output formatting
# --------------------------------------------------------------------------


def format_amount(amount: CurrencyAmount | None) -> str:
    if amount is None:
        return NOT_AVAILABLE
    return f"{amount.amount} {amount.currency}"


def format_date(value: date | None) -> str:
    return value.isoformat() if value is not None else NOT_AVAILABLE


def format_credit_line(credit: Credit, snapshot_date: date | None = None) -> str:
    """``creditId accountId creditStatus lifecycle remainingAmount endDate``.

    The Lifecycle_Status is derived at ``snapshot_date``; when not
    provided, today's UTC date is used so ad-hoc CLI runs still produce a
    meaningful label without requiring the snapshot to be threaded through
    every call site.
    """
    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).date()
    lifecycle = lifecycle_status(credit, snapshot_date).value
    return " ".join(
        (
            credit.creditId or NOT_AVAILABLE,
            credit.accountId or NOT_AVAILABLE,
            credit.creditStatus or NOT_AVAILABLE,
            lifecycle,
            format_amount(credit.remainingAmount),
            format_date(credit.endDate),
        )
    )


def format_success(result: RunResult) -> list[str]:
    """Render the stdout lines for a successful run."""
    lines: list[str] = []
    if not result.credits:
        lines.append("Zero credits were retrieved.")
    else:
        snapshot_date = result.snapshot_date
        lines.extend(format_credit_line(credit, snapshot_date) for credit in result.credits)

    total = f"{result.total_remaining}"
    if result.remaining_currency:
        total = f"{total} {result.remaining_currency}"
    lines.append(f"Total credits: {result.total_count}")
    lines.append(f"Total remaining: {total}")
    if result.snapshot_key:
        lines.append(f"Snapshot written: {result.snapshot_key}")
    if result.dashboard is not None:
        lines.append(format_dashboard_line(result.dashboard))
    lines.extend(result.messages)
    return lines


def format_dashboard_line(dashboard: Dashboard) -> str:
    """One-line summary: active, expiring, and newly issued counts."""
    return (
        f"Dashboard: {dashboard.active_count} active "
        f"(total remaining {dashboard.active_total_display()}), "
        f"{dashboard.this_month_count} expiring this month, "
        f"{dashboard.next_three_count} expiring in the next three months, "
        f"{dashboard.new_count} newly issued"
    )


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def run(
    args: argparse.Namespace,
    orchestrator: _OrchestratorLike,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Invoke the orchestrator once and map the result to an exit code."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    result = orchestrator.run()
    if not result.success:
        message = result.error_message or "run failed without a recorded error message"
        print(f"error: {message}", file=err)
        # Surface any context gathered before the failure (for example missing
        # field notes when retrieval succeeded but the snapshot write failed).
        for note in result.messages:
            print(note, file=err)
        return EXIT_FAILURE

    for line in format_success(result):
        print(line, file=out)

    # Notification outcome. The NotifyResult messages are
    # already part of ``result.messages`` above; a delivery failure is echoed
    # to stderr so it is not lost in a redirected stdout, but the run itself
    # succeeded (snapshot written) so the exit code stays 0.
    if not notification_requested(args):
        print(NOTIFICATION_SKIPPED_MESSAGE, file=out)
    elif result.delivery_failed:
        reason = (
            result.notify_result.failure_reason
            if result.notify_result is not None and result.notify_result.failure_reason
            else "no reason recorded"
        )
        print(f"warning: notification delivery failed: {reason}", file=err)

    return EXIT_SUCCESS


def main(
    argv: Sequence[str] | None = None,
    orchestrator_factory: Callable[[argparse.Namespace], _OrchestratorLike] | None = None,
) -> int:
    """Console-script entry point. Returns the process exit code."""
    args = parse_args(argv)
    factory = orchestrator_factory if orchestrator_factory is not None else build_orchestrator
    try:
        orchestrator = factory(args)
    except SetupError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILURE
    return run(args, orchestrator)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
