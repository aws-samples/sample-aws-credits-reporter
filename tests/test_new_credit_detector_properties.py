"""Property tests for ``aws_credits_reporter.new_credit_detector`` (Task 10.2).

Property 6 / design Property 12 (Validates: Requirements 6.2, 6.6, 6.7):

    For any current and prior creditId sets, ``new_credit_ids`` equals exactly
    the set difference current minus prior; with no prior snapshot or a read
    error the set is empty and the corresponding flag is set.

The detector diffs the current snapshot's creditId set against the most recent
readable prior snapshot's set (Requirement 6.2). Two degraded modes short the
diff: a missing prior is an initial baseline (Requirement 6.6), and an
unreadable prior (``prior_read_ok`` False) is a comparison error that takes
precedence over a missing prior (Requirement 6.7). In both degraded modes
nothing is classified as new.

No mocking library is used: snapshots are built from raw ``get-credits`` page
dicts exactly as the real pipeline stores them, and the detector parses them
through the production ``parse_credits`` path.
"""

from __future__ import annotations

from datetime import datetime, timezone

from hypothesis import given, settings, strategies as st

from aws_credits_reporter.models import Snapshot
from aws_credits_reporter.new_credit_detector import NewCreditDetector

# creditId values are drawn from a small alphabet so the current and prior sets
# actually overlap often enough to exercise every region of the set difference
# (ids only in current, only in prior, in both).
_credit_ids = st.sets(
    st.sampled_from([f"credit-{i:02d}" for i in range(12)]),
    max_size=8,
)


def _snapshot_from_ids(ids: set[str], key: str) -> Snapshot:
    """Build a Snapshot whose ``get-credits`` output carries exactly ``ids``.

    Ids are spread across a couple of raw pages, mirroring pagination, so the
    detector's ``parse_credits`` flattening is exercised too.
    """
    id_list = sorted(ids)
    mid = len(id_list) // 2
    pages = [
        {"credits": [{"creditId": cid} for cid in id_list[:mid]]},
        {"credits": [{"creditId": cid} for cid in id_list[mid:]]},
    ]
    return Snapshot(
        key=key,
        taken_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        raw_get_credits=pages,
    )


@settings(max_examples=300)
@given(current_ids=_credit_ids, prior_ids=_credit_ids)
def test_new_credit_ids_equal_current_minus_prior(
    current_ids: set[str], prior_ids: set[str]
) -> None:
    """With a readable prior, new_credit_ids == current − prior exactly."""
    current = _snapshot_from_ids(current_ids, key="snapshots/current.json")
    prior = _snapshot_from_ids(prior_ids, key="snapshots/prior.json")

    result = NewCreditDetector().detect(current, prior)

    assert result.new_credit_ids == current_ids - prior_ids
    # The flagged degraded modes are off on the happy path.
    assert result.is_initial_baseline is False
    assert result.comparison_error is False
    # new_credits mirrors the id set (order preserved from the current snapshot).
    assert {c.creditId for c in result.new_credits} == current_ids - prior_ids


@settings(max_examples=200)
@given(current_ids=_credit_ids)
def test_no_prior_is_baseline_with_empty_new_set(current_ids: set[str]) -> None:
    """A missing prior yields the initial-baseline flag and no new credits."""
    current = _snapshot_from_ids(current_ids, key="snapshots/current.json")

    result = NewCreditDetector().detect(current, None)

    assert result.new_credit_ids == set()
    assert result.new_credits == []
    assert result.is_initial_baseline is True
    assert result.comparison_error is False


@settings(max_examples=200)
@given(current_ids=_credit_ids, prior_ids=_credit_ids, has_prior=st.booleans())
def test_read_error_is_comparison_error_and_takes_precedence(
    current_ids: set[str], prior_ids: set[str], has_prior: bool
) -> None:
    """prior_read_ok False flags a comparison error, whether or not a prior
    object is supplied, and always yields an empty new set."""
    current = _snapshot_from_ids(current_ids, key="snapshots/current.json")
    prior = (
        _snapshot_from_ids(prior_ids, key="snapshots/prior.json")
        if has_prior
        else None
    )

    result = NewCreditDetector().detect(current, prior, prior_read_ok=False)

    assert result.new_credit_ids == set()
    assert result.new_credits == []
    assert result.comparison_error is True
    # Comparison error wins over the missing-prior baseline.
    assert result.is_initial_baseline is False
