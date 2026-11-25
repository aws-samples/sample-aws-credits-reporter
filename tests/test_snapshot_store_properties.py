"""Property tests for ``aws_credits_reporter.snapshot_store.SnapshotStore.make_key``.

Property 3 (design Property 13): Snapshot key order equals chronological order.

For any two UTC datetimes ``a < b``, ``make_key(a) < make_key(b)``
lexicographically, so listing a prefix and sorting keys yields chronological
order (Requirement 2.2). For equal datetimes, distinct collision suffixes never
produce equal keys, so the write path can always pick a fresh, non-overwriting
name (Requirement 2.3).

``make_key`` formats the timestamp with a one-second-resolution ISO 8601 basic
stamp (``%Y%m%dT%H%M%SZ``), so chronological ordering is expressed at whole-second
granularity: two datetimes in the same second map to the same stamp. The
generators below therefore compare datetimes at whole-second resolution, which
is exactly the resolution the key preserves.

Uses the ``hypothesis`` library.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, strategies as st

from aws_credits_reporter.snapshot_store import SnapshotStore


def _store() -> SnapshotStore:
    # make_key needs only the prefix; no S3 calls are made by these tests.
    return SnapshotStore(s3_client=None, bucket="bucket", prefix="snapshots/")


# Aware UTC datetimes truncated to whole seconds, spanning a wide range so the
# stamp's year/month/day/time fields are all exercised. datetime ordering at
# this resolution is exactly what the key encodes.
_utc_seconds = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2099, 12, 31, 23, 59, 59),
    timezones=st.just(timezone.utc),
).map(lambda dt: dt.replace(microsecond=0))


# --------------------------------------------------------------------------
# Property 3 part 1: a < b (at second resolution) => make_key(a) < make_key(b)
# **Validates: Requirements 2.2**
# --------------------------------------------------------------------------


@given(a=_utc_seconds, b=_utc_seconds)
def test_key_order_matches_chronological_order(a: datetime, b: datetime) -> None:
    store = _store()
    key_a = store.make_key(a)
    key_b = store.make_key(b)
    if a < b:
        assert key_a < key_b
    elif a > b:
        assert key_a > key_b
    else:
        # Same instant at second resolution -> identical key.
        assert key_a == key_b


@given(base=_utc_seconds, delta_seconds=st.integers(min_value=1, max_value=10**9))
def test_later_datetime_yields_strictly_greater_key(
    base: datetime, delta_seconds: int
) -> None:
    """A strictly later timestamp always sorts strictly after the earlier one."""
    store = _store()
    later = base + timedelta(seconds=delta_seconds)
    assert store.make_key(base) < store.make_key(later)


# --------------------------------------------------------------------------
# Property 3 part 2: for equal datetimes, distinct suffixes never compare equal
# **Validates: Requirements 2.3**
# --------------------------------------------------------------------------


@given(
    taken_at=_utc_seconds,
    suffixes=st.lists(
        st.integers(min_value=1, max_value=1000),
        min_size=2,
        max_size=12,
        unique=True,
    ),
)
def test_distinct_suffixes_never_collide(
    taken_at: datetime, suffixes: list[int]
) -> None:
    store = _store()
    keys = [store.make_key(taken_at, suffix) for suffix in suffixes]
    # Distinct suffixes on one timestamp must yield distinct keys, and none of
    # them may equal the unsuffixed key for that same timestamp.
    assert len(set(keys)) == len(keys)
    assert store.make_key(taken_at) not in keys
