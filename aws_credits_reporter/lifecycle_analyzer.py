"""Lifecycle_Analyzer: derive Lifecycle_Status and partition credits.

The analyzer is the single place that encodes the Billing console's lifecycle
semantics for the run. It evaluates every credit against the Snapshot_Date
using the pure ``lifecycle_status`` function in ``aws_credits_reporter.models``
 and splits the input into two lists:

* ``active``   - credits whose Lifecycle_Status is Active. This is the input
  to ``ExpirationAnalyzer.classify``, so no
  Expired, Exhausted, or Disabled credit can ever enter an
  expiration tier.
* ``inactive`` - credits whose Lifecycle_Status is Expired, Exhausted, or
  Disabled.

Both lists preserve the input order. ``status_by_id`` maps each credit's
``creditId`` to its derived status; credits without a ``creditId`` still
appear in ``active`` / ``inactive`` but are absent from the id map because
they cannot be looked up by id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import Credit, LifecycleStatus, lifecycle_status


@dataclass(frozen=True)
class LifecycleResult:
    """The outcome of partitioning one credit list at ``snapshot_date``.

    Every credit appears in exactly one of ``active`` / ``inactive``.
    ``status_by_id`` carries the derived Lifecycle_Status keyed on
    ``creditId``; credits without a ``creditId`` are still placed in the
    appropriate list but are omitted from the id map.
    """

    snapshot_date: date
    status_by_id: dict[str, LifecycleStatus] = field(default_factory=dict)
    active: list[Credit] = field(default_factory=list)
    inactive: list[Credit] = field(default_factory=list)


class LifecycleAnalyzer:
    """Derives Lifecycle_Status for each credit at the Snapshot_Date."""

    def __init__(self, snapshot_date: date):
        self.snapshot_date = snapshot_date

    def status_of(self, credit: Credit) -> LifecycleStatus:
        """Return the Lifecycle_Status of ``credit`` at ``snapshot_date``.

        Thin wrapper over ``lifecycle_status(credit, self.snapshot_date)`` so
        callers that only need one status do not have to remember the pure
        function's signature.
        """
        return lifecycle_status(credit, self.snapshot_date)

    def partition(self, credits: list[Credit]) -> LifecycleResult:
        """Split ``credits`` into Active_Credits and Inactive_Credits.

        Iterates once, derives each credit's status, appends to ``active`` or
        ``inactive`` preserving the input order, and populates
        ``status_by_id`` for credits that carry a ``creditId``.
        """
        active: list[Credit] = []
        inactive: list[Credit] = []
        status_by_id: dict[str, LifecycleStatus] = {}
        for credit in credits:
            status = lifecycle_status(credit, self.snapshot_date)
            if credit.creditId is not None:
                status_by_id[credit.creditId] = status
            if status is LifecycleStatus.ACTIVE:
                active.append(credit)
            else:
                inactive.append(credit)
        return LifecycleResult(
            snapshot_date=self.snapshot_date,
            status_by_id=status_by_id,
            active=active,
            inactive=inactive,
        )
