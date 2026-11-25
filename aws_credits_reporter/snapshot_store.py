"""S3-backed snapshot persistence for AWS Credits Reporter.

A snapshot is a single JSON object holding the unmodified raw response pages
of ``get-credits`` and ``list-cost-category-definitions``.
Keys are named with a UTC ISO 8601 basic timestamp, with a numeric ``-<n>``
suffix on collision, so ordering by (timestamp, suffix) is chronological. Existing objects are never
overwritten: on collision a distinct ``-<n>`` suffix is chosen.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from botocore.exceptions import BotoCoreError, ClientError

from aws_credits_reporter.aws_calls import call_with_backoff
from aws_credits_reporter.models import (
    SNAPSHOT_SOURCE_MANUAL,
    SNAPSHOT_SOURCE_SCHEDULED,
    SNAPSHOT_SOURCES,
    Snapshot,
)

_KEY_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_PRECONDITION_FAILED_CODES = {"412", "PreconditionFailed"}
# Upper bound on ``-<n>`` suffixes tried for one timestamp before giving up.
_MAX_COLLISION_SUFFIX = 1000
_SNAPSHOT_SUFFIX = ".json"
# Baseline validation. A snapshot written by this tool is
# stored within a few minutes of its ``taken_at``; the store rejects anything
# S3 says was written noticeably before that time (impossible) or long after it
# (a snapshot planted under an earlier-looking key).
_MAX_CLOCK_SKEW = timedelta(seconds=60)
_MAX_WRITE_DELAY = timedelta(hours=1)
# How many of the newest valid snapshots are examined looking for a scheduled
# one before falling back to the newest manual one.
_MAX_BASELINE_LOOKBACK = 30


class SnapshotReadError(Exception):
    """Raised when a snapshot listing or object read fails.

    Not fatal for the run: callers record the failure (for example as a
    comparison error) and continue.
    """

    def __init__(self, key: str, message: str) -> None:
        self.key = key
        super().__init__(f"failed to read snapshot {key!r}: {message}")


class SnapshotWriteError(Exception):
    """Raised when a snapshot could not be written after all retries.

    Fatal for the run. ``last_error`` carries the message of
    the final underlying failure.
    """

    def __init__(self, key: str, attempts: int, last_error: str) -> None:
        self.key = key
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"failed to write snapshot {key!r} after {attempts} attempt(s): {last_error}"
        )


def _to_utc(taken_at: datetime) -> datetime:
    """Return ``taken_at`` as an aware UTC datetime (naive values are taken as UTC)."""
    if taken_at.tzinfo is None:
        return taken_at.replace(tzinfo=timezone.utc)
    return taken_at.astimezone(timezone.utc)


def _error_code(error: ClientError) -> str:
    response = getattr(error, "response", None) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    if not code:
        code = str(response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
    return code


def _chronological_key(key: str) -> tuple[str, int]:
    """Sort key placing ``<stamp>-<n>.json`` after ``<stamp>.json``.

    Plain string order is wrong for same-second collisions: ``-`` (0x2d) sorts
    before ``.`` (0x2e), so ``Z-1.json`` would sort before ``Z.json`` even
    though it was written later. Names that do not match the pattern sort by
    the whole name with suffix 0.
    """
    name = key[: -len(_SNAPSHOT_SUFFIX)] if key.endswith(_SNAPSHOT_SUFFIX) else key
    stamp, sep, suffix = name.rpartition("-")
    if sep and suffix.isdigit():
        return stamp, int(suffix)
    return name, 0


def _json_default(value: Any) -> str:
    """Encode the non-JSON types boto3 puts in raw responses (timestamps, Decimal)."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


class SnapshotStore:
    """Reads and writes raw snapshots in S3 (the sole persistence store)."""

    def __init__(
        self,
        s3_client,
        bucket: str,
        prefix: str = "snapshots/",
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        expected_bucket_owner: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._s3 = s3_client
        self._bucket = bucket
        self._prefix = prefix
        # Account id that must own the bucket. Sent as
        # ExpectedBucketOwner on every S3 call, so S3 rejects the request with
        # a 403 when the name belongs to another account instead of handing
        # snapshots to a look-alike bucket.
        self._expected_owner = expected_bucket_owner or None
        self._now = now or (lambda: datetime.now(timezone.utc))
        #: ``(key, reason)`` of snapshots skipped as baseline by the last
        #: ``read_latest_prior`` call.
        self.rejected: list[tuple[str, str]] = []
        self._max_retries = max(1, max_retries)
        self._sleep = sleep

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def prefix(self) -> str:
        return self._prefix

    def _owner_params(self) -> dict[str, str]:
        """``ExpectedBucketOwner`` for S3 calls, empty when no owner is configured."""
        if self._expected_owner:
            return {"ExpectedBucketOwner": self._expected_owner}
        return {}

    # ------------------------------------------------------------------
    # Naming and (de)serialization
    # ------------------------------------------------------------------

    def make_key(self, taken_at: datetime, suffix: int | None = None) -> str:
        """Build ``<prefix>YYYYMMDDTHHMMSSZ.json`` (``-<suffix>.json`` when given)."""
        stamp = _to_utc(taken_at).strftime(_KEY_TIMESTAMP_FORMAT)
        if suffix is None:
            return f"{self._prefix}{stamp}.json"
        return f"{self._prefix}{stamp}-{suffix}.json"

    @staticmethod
    def serialize(snapshot: Snapshot) -> bytes:
        """Encode a snapshot as a single JSON document with the raw pages unmodified."""
        document = {
            "taken_at": _to_utc(snapshot.taken_at).isoformat(),
            "source": snapshot.source,
            "get_credits": list(snapshot.raw_get_credits),
            "cost_categories": list(snapshot.raw_cost_categories),
        }
        return json.dumps(document, default=_json_default).encode("utf-8")

    @staticmethod
    def deserialize(key: str, data: bytes) -> Snapshot:
        """Decode a snapshot document. Tolerates a missing ``cost_categories`` section."""
        document = json.loads(data.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"snapshot {key!r} is not a JSON object")
        raw_taken_at = document.get("taken_at")
        if not isinstance(raw_taken_at, str):
            raise ValueError(f"snapshot {key!r} has no 'taken_at' timestamp")
        taken_at = datetime.fromisoformat(raw_taken_at.replace("Z", "+00:00"))
        get_credits = document.get("get_credits") or []
        cost_categories = document.get("cost_categories") or []
        if not isinstance(get_credits, list) or not isinstance(cost_categories, list):
            raise ValueError(f"snapshot {key!r} has malformed page lists")
        # A document without ``source`` predates the field and was written by
        # the only runner there was, so it counts as scheduled. An unknown value
        # is treated as manual, the safe side for baseline selection.
        raw_source = document.get("source", SNAPSHOT_SOURCE_SCHEDULED)
        source = raw_source if raw_source in SNAPSHOT_SOURCES else SNAPSHOT_SOURCE_MANUAL
        return Snapshot(
            key=key,
            taken_at=_to_utc(taken_at),
            raw_get_credits=get_credits,
            raw_cost_categories=cost_categories,
            source=source,
        )

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def write(self, snapshot: Snapshot) -> str:
        """Write ``snapshot`` under a free key, set ``snapshot.key``, and return it.

        Never overwrites: every put is conditional (``IfNoneMatch="*"``), so S3
        itself rejects a key that already exists with a 412 and the next distinct
        suffix (``-1``, ``-2``, ...) is tried. No ``head_object`` pre-check is
        made, so the runtime role needs only ``s3:PutObject`` for this step.
        ``put_object`` is retried with exponential backoff; persistent failure
        raises ``SnapshotWriteError``.
        """
        body = self.serialize(snapshot)
        suffix: int | None = None
        while True:
            key = self.make_key(snapshot.taken_at, suffix)
            if self._put_with_retry(key, body):
                snapshot.key = key
                return key
            suffix = 1 if suffix is None else suffix + 1
            if suffix > _MAX_COLLISION_SUFFIX:
                raise SnapshotWriteError(
                    key,
                    _MAX_COLLISION_SUFFIX,
                    f"no free snapshot key found after {_MAX_COLLISION_SUFFIX} suffixes",
                )

    def _put_with_retry(self, key: str, body: bytes) -> bool:
        """Put ``body`` at ``key``. Returns False on a 412 collision, True on success."""
        params = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": body,
            "ContentType": "application/json",
            "IfNoneMatch": "*",
            **self._owner_params(),
        }

        def is_retryable(error: BaseException) -> bool:
            if isinstance(error, ClientError):
                return _error_code(error) not in _PRECONDITION_FAILED_CODES
            return isinstance(error, BotoCoreError)

        try:
            call_with_backoff(
                lambda: self._s3.put_object(**params),
                attempts=self._max_retries,
                is_retryable=is_retryable,
                sleep=self._sleep,
            )
        except ClientError as error:
            if _error_code(error) in _PRECONDITION_FAILED_CODES:
                return False
            raise SnapshotWriteError(key, self._max_retries, str(error)) from error
        except BotoCoreError as error:
            raise SnapshotWriteError(key, self._max_retries, str(error)) from error
        return True

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def _list_keys(self) -> list[str]:
        """Return every snapshot key under the prefix, sorted ascending.

        Pages through ``list_objects_v2`` with the continuation token. Only
        objects whose key starts with the prefix and ends in ``.json`` are
        considered snapshots; anything else under the prefix is ignored.
        Listing failures raise ``SnapshotReadError``.
        """
        keys: list[str] = []
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": self._prefix,
            **self._owner_params(),
        }
        while True:
            try:
                page = self._s3.list_objects_v2(**params)
            except (ClientError, BotoCoreError) as error:
                raise SnapshotReadError(
                    self._prefix, f"list_objects_v2 failed: {error}"
                ) from error
            for obj in page.get("Contents") or []:
                key = obj.get("Key")
                if not isinstance(key, str):
                    continue
                if key.startswith(self._prefix) and key.endswith(_SNAPSHOT_SUFFIX):
                    keys.append(key)
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not token:
                break
            params["ContinuationToken"] = token
        keys.sort(key=_chronological_key)
        return keys

    def read(self, key: str) -> Snapshot:
        """Fetch and deserialize the snapshot stored at ``key``.

        Raises ``SnapshotReadError`` when the object cannot be fetched or its
        contents are not a valid snapshot document.
        """
        snapshot, _ = self._read_with_modified(key)
        return snapshot

    def _read_with_modified(self, key: str) -> tuple[Snapshot, datetime | None]:
        """Like ``read`` but also return S3's ``LastModified`` for the object."""
        try:
            response = self._s3.get_object(
                Bucket=self._bucket, Key=key, **self._owner_params()
            )
            data = response["Body"].read()
        except (ClientError, BotoCoreError) as error:
            raise SnapshotReadError(key, f"get_object failed: {error}") from error
        except (KeyError, AttributeError) as error:
            raise SnapshotReadError(key, f"malformed get_object response: {error}") from error
        try:
            snapshot = self.deserialize(key, data)
        except (ValueError, UnicodeDecodeError) as error:
            raise SnapshotReadError(key, f"could not decode snapshot: {error}") from error
        modified = response.get("LastModified")
        if isinstance(modified, datetime):
            modified = _to_utc(modified)
        else:
            modified = None
        return snapshot, modified

    def _validate_baseline(
        self, snapshot: Snapshot, modified: datetime | None, now: datetime
    ) -> str | None:
        """Return why ``snapshot`` cannot be trusted as a baseline, or ``None``.

        The baseline decides which credits count as new, so
        a snapshot planted under a forged key must not become the baseline. The
        checks use only facts the writer cannot choose or that must agree with
        each other: the key name against the ``taken_at`` inside the document,
        both against the current time, and S3's own ``LastModified``.
        """
        stamp_text, _ = _chronological_key(snapshot.key.rsplit("/", 1)[-1])
        try:
            key_time = datetime.strptime(stamp_text, _KEY_TIMESTAMP_FORMAT).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return "key name is not a snapshot timestamp"
        taken_at = _to_utc(snapshot.taken_at)
        if abs(taken_at - key_time) >= timedelta(seconds=1):
            return "taken_at does not match the key timestamp"
        if taken_at > now + _MAX_CLOCK_SKEW:
            return "taken_at is in the future"
        if modified is not None:
            if modified < taken_at - _MAX_CLOCK_SKEW:
                return "S3 LastModified is earlier than taken_at"
            if modified > taken_at + _MAX_WRITE_DELAY:
                return "S3 LastModified is long after taken_at"
        return None

    def read_latest_prior(self, before_key: str | None = None) -> Snapshot | None:
        """Return the most recent valid snapshot with key strictly less than ``before_key``.

        When ``before_key`` is ``None`` the latest snapshot overall is returned.
        Returns ``None`` when no such snapshot exists: a
        first run has no baseline and continues normally. Errors listing the
        prefix or reading the chosen object raise ``SnapshotReadError`` so the
        caller can record the comparison failure without aborting the run.

        Candidates are checked newest first by ``_validate_baseline``; one that
        fails is skipped, recorded in ``rejected``, and the next older one is
        tried. Nothing is deleted.

        Snapshots flagged ``manual`` (CLI runs and ad-hoc
        invocations) are passed over in favour of the newest ``scheduled``
        one, so a manual run cannot consume the next scheduled run's new-credit
        alert. When none of the newest ``_MAX_BASELINE_LOOKBACK`` valid
        snapshots is scheduled (a CLI-only setup, or a fresh deployment), the
        newest valid manual one is used, which keeps the CLI working on its own.
        """
        self.rejected = []
        keys = self._list_keys()
        if before_key is not None:
            cutoff = _chronological_key(before_key)
            keys = [key for key in keys if _chronological_key(key) < cutoff]
        now = _to_utc(self._now())
        newest_manual: Snapshot | None = None
        examined = 0
        for key in reversed(keys):
            snapshot, modified = self._read_with_modified(key)
            reason = self._validate_baseline(snapshot, modified, now)
            if reason is not None:
                self.rejected.append((key, reason))
                continue
            if snapshot.source != SNAPSHOT_SOURCE_MANUAL:
                return snapshot
            if newest_manual is None:
                newest_manual = snapshot
            examined += 1
            if examined >= _MAX_BASELINE_LOOKBACK:
                break
        return newest_manual
